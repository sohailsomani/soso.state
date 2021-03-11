import copy
import logging
import math
import traceback
import typing
from collections import defaultdict
from dataclasses import dataclass, field, is_dataclass
from typing import ClassVar

from soso.state import protocols
from soso.state.event import (Event, EventCallback, EventToken, _DummyLogger,
                              _LoggerInterface)

__all__ = ['Model', 'StateT', 'T', 'PropertyCallback', 'build_model', 'initialize_logging']

StateT_contra = typing.TypeVar('StateT_contra', contravariant=True)
StateT = typing.TypeVar('StateT')
RootStateT = typing.TypeVar('RootStateT')
T = typing.TypeVar('T')
T_contra = typing.TypeVar('T_contra', contravariant=True)
T_co = typing.TypeVar('T_co', covariant=True)

PropertyCallback = typing.Callable[[StateT_contra], T_co]
StateUpdateCallback = typing.Callable[[StateT_contra], None]


def initialize_logging() -> None:
    Model._logger = logging.getLogger(__name__)
    Event._initialize_logging()


class PropertyOp(typing.Protocol):
    @property
    def key(self) -> typing.Any:
        ...

    def execute(self, obj: typing.Any) -> typing.Tuple[typing.Optional[typing.Any], bool]:
        ...

    def execute_raw(self, obj: typing.Any) -> typing.Optional[typing.Any]:
        ...

    def get_value(self, obj: typing.Any) -> typing.Optional[typing.Any]:
        ...


@dataclass
class Node:
    children: typing.DefaultDict[str, "Node"] = field(default_factory=lambda: defaultdict(Node))
    event: Event[typing.Any] = field(default_factory=lambda: Event("NodeUpdateEvent"))
    # The type of access to this node
    op: typing.Optional[PropertyOp] = None


class Model(typing.Generic[StateT], protocols.Model[StateT]):
    _logger: ClassVar[_LoggerInterface] = _DummyLogger()

    def __init__(self, initial_state: StateT) -> None:
        self.__state_klass = state_klass = initial_state.__class__
        if not is_dataclass(state_klass):
            raise ValueError("Expected a dataclass, got %s" % state_klass)
        assert is_dataclass(state_klass)
        self.__current_state = copy.deepcopy(initial_state)
        self.__root_node = Node()
        self.__root_node.event._name = "root"

    def __get_node_for_ops(self, ops: typing.List[PropertyOp]) -> Node:
        root: Node = self.__root_node
        s = root.event._name
        for op in ops:
            root = root.children[op.key]
            root.op = op
            s += '.' + str(op.key)
            root.event._name = s
        return root

    def __get_value_for_ops(self, ops: typing.List[PropertyOp]) -> typing.Any:
        root: typing.Any = self.__current_state
        for op in ops:
            root = op.get_value(root)
        return root

    def submodel(self, func: PropertyCallback[StateT, T]) -> protocols.Model[T]:
        return _SubModel(self, func)

    def observe(self, callback: EventCallback[StateT]) -> EventToken:
        return self.observe_property(lambda x: x, callback)

    def observe_property(self, func: PropertyCallback[StateT, T],
                         callback: EventCallback[T]) -> EventToken:
        event, ops = self.__event(func)
        token = event.connect(callback)
        try:
            value = self.__get_value_for_ops(ops)
            # call with the initial value
            callback(value)
        # Can fail for many reasons (value doesn't exist yet is a common one),
        # swallow but log exception
        except Exception as e:
            self._logger.exception(e)
        return token

    def _get_submodel_root(self) -> typing.Callable[[StateT], typing.Any]:
        return lambda x: x

    def update_state(self, func: StateUpdateCallback[StateT]) -> None:
        self._update_state(lambda x: x, func)

    def update_state_root(self, root: typing.Callable[[StateT], T],
                          func: StateUpdateCallback[T]) -> None:
        return self._update_state(root, func)

    def update_properties(self, **kwargs: typing.Any) -> None:
        def update(state: StateT) -> None:
            for key, value in kwargs.items():
                setattr(state, key, value)

        self.update_state(update)

    @property
    # TODO: this should return a read-only view to avoid accidents
    def state(self) -> StateT:
        return self.__current_state

    def wait_for(self) -> Event[StateT]:
        return self.wait_for_property(lambda x: x)

    def wait_for_property(self, property: PropertyCallback[StateT, T]) -> Event[T]:
        return self.event(property)

    def snapshot(self) -> StateT:
        return self.snapshot_property(lambda x: x)

    def snapshot_property(self, property: typing.Optional[PropertyCallback[StateT, T]] = None) -> T:
        if property is None:

            def cb(x: StateT) -> StateT:
                return x

            property = cb  # type: ignore
        assert property is not None
        subtree = property(self.state)
        return copy.deepcopy(subtree)

    def restore(self, snapshot: StateT) -> None:
        self.__current_state = copy.deepcopy(snapshot)
        self.__fire_all_child_events(self.__root_node, self.__current_state)

    def restore_property(self, snapshot: T, property: PropertyCallback[StateT, T]) -> None:
        proxy = self.__make_proxy()
        property(proxy)
        ops = self.__get_ops(proxy)
        assert len(ops) > 0
        if isinstance(ops[-1], GetAttr):
            ops[-1] = SetAttr(ops[-1].key, snapshot)
        else:
            assert isinstance(ops[-1], GetItem)
            ops[-1] = SetItem(ops[-1].key, snapshot)

        def update(state: StateT) -> None:
            obj: typing.Optional[typing.Any] = state
            for op in ops:
                obj = op.execute_raw(obj)

        self.update_state(update)

    def _update_state(self, root: typing.Callable[[StateT], T],
                      func: StateUpdateCallback[T]) -> None:

        # Get all changes
        proxy = self.__make_proxy()
        func(root(proxy))
        ops = self.__get_ops(proxy)
        obj: typing.Optional[typing.Any] = root(self.__current_state)

        # Apply changes tos tate
        stmts: typing.List[typing.List[PropertyOp]] = []
        curr_stmt: typing.List[PropertyOp] = []
        self._logger.debug("Update ops: %s", ops)
        for op in ops:
            curr_stmt.append(op)
            obj, changed = op.execute(obj)
            # end of statement
            if obj is None:
                obj = root(self.__current_state)
                if changed:
                    stmts.append(curr_stmt)
                    curr_stmt = []
                else:
                    # if not changed, ignore this statement
                    curr_stmt = []

        # if the last expression had no set, then that means it was a read
        # without a write. No good. Let the user know.
        assert not curr_stmt

        if len(stmts) == 0:
            return

        # Always emit root
        self.__root_node.event.emit(self.__current_state)

        # Emit everything from actual root to __submodel_root
        rootproxy = self.__make_proxy()
        root(rootproxy)
        rootops = self.__get_ops(rootproxy)
        curr = []
        for op in rootops:
            curr.append(op)
            node = self.__get_node_for_ops(curr)
            value = self.__get_value_for_ops(curr)
            node.event.emit(value)

        # Now emit the fields that were actually modified
        for stmt in stmts:
            assert stmt
            # if foo.bar.baz[0] is modified then we need to signal foo,
            # foo.bar, foo.bar, foo.bar.baz[0], and then everything
            # below foo.bar.baz[0]
            curr = []
            for op in stmt:
                curr.append(op)
                node = self.__get_node_for_ops(curr)
                value = self.__get_value_for_ops(curr)
                node.event.emit(value)
            # Now everything below node
            self.__fire_all_child_events(node, value)

    def __make_proxy(self) -> StateT:
        return Proxy()  # type: ignore

    def __get_ops(self, proxy: T) -> typing.List[PropertyOp]:
        return proxy.__dict__['__ops']  # type: ignore

    def __event(
            self, func: PropertyCallback[StateT,
                                         T]) -> typing.Tuple[Event[T], typing.List[PropertyOp]]:
        proxy: StateT = self.__make_proxy()
        func(proxy)
        ops = self.__get_ops(proxy)

        node = self.__get_node_for_ops(ops)
        return node.event, ops

    def event(self, property: PropertyCallback[StateT, T]) -> Event[T]:
        return self.__event(property)[0]

    def __fire_all_child_events(self, node: Node, parent: typing.Any) -> None:
        self._logger.debug("Firing all child events: %s", node.event._name)
        for name, child_node in node.children.items():
            try:
                assert child_node.op is not None
                child_value = child_node.op.get_value(parent)
                child_node.event.emit(child_value)
                self.__fire_all_child_events(child_node, child_value)
            except Exception:
                # It's common for values to disappear, no need to pepper
                # info logs. TODO: should GC such child nodes? Probably
                self._logger.debug(traceback.format_exc())

    def __str__(self) -> str:
        return f"#<Model state={self.state}>"

    def __repr__(self) -> str:
        return str(self)


@dataclass
class GetAttr:
    key: typing.Any

    def execute(self, obj: typing.Any) -> typing.Tuple[typing.Optional[typing.Any], bool]:
        return getattr(obj, self.key), False

    def execute_raw(self, obj: typing.Any) -> typing.Optional[typing.Any]:
        return getattr(obj, self.key)

    def get_value(self, obj: typing.Any) -> typing.Optional[typing.Any]:
        return getattr(obj, self.key)


@dataclass
class SetAttr:
    key: typing.Any
    value: typing.Any

    def execute(self, obj: typing.Any) -> typing.Tuple[typing.Optional[typing.Any], bool]:
        curr_value = getattr(obj, self.key)
        changed = curr_value != self.value
        if changed and isinstance(curr_value, float):
            # At least one should not be NaN
            changed = not math.isnan(curr_value) or not math.isnan(self.value)
        if changed:
            setattr(obj, self.key, self.value)
        return None, changed

    def execute_raw(self, obj: typing.Any) -> typing.Optional[typing.Any]:
        setattr(obj, self.key, self.value)
        return None

    def get_value(self, obj: typing.Any) -> typing.Optional[typing.Any]:
        return getattr(obj, self.key)


@dataclass
class GetItem:
    key: typing.Any

    def execute(self, obj: typing.Any) -> typing.Tuple[typing.Optional[typing.Any], bool]:
        return obj[self.key], False

    def execute_raw(self, obj: typing.Any) -> typing.Optional[typing.Any]:
        return obj[self.key]

    def get_value(self, obj: typing.Any) -> typing.Optional[typing.Any]:
        return obj[self.key]


@dataclass
class SetItem:
    key: typing.Any
    value: typing.Any

    def execute(self, obj: typing.Any) -> typing.Tuple[typing.Optional[typing.Any], bool]:
        try:
            curr_value = obj[self.key]
            changed = curr_value != self.value
            if changed and isinstance(curr_value, float):
                # At least one should not be NaN
                changed = not math.isnan(curr_value) or not math.isnan(self.value)
        except KeyError:
            changed = True
        if changed:
            obj[self.key] = self.value
        return None, changed

    def execute_raw(self, obj: typing.Any) -> typing.Optional[typing.Any]:
        obj[self.key] = self.value
        return None

    def get_value(self, obj: typing.Any) -> typing.Any:
        return obj[self.key]


@dataclass
class Call:
    args: typing.Tuple[typing.Any, ...]
    kwargs: typing.Dict[str, typing.Any]
    key: str = '__call__'

    def execute(self, obj: typing.Any) -> typing.Tuple[typing.Optional[typing.Any], bool]:
        return obj(*self.args, **self.kwargs), True

    def execute_raw(self, obj: typing.Any) -> typing.Optional[typing.Any]:
        return obj(*self.args, **self.kwargs)

    def get_value(self, obj: typing.Any) -> typing.Any:
        return obj.__call__


class Proxy:
    def __init__(self) -> None:
        self.__dict__['__ops'] = []

    def __setattr__(self, name: str, value: typing.Any) -> None:
        self.__dict__['__ops'].append(SetAttr(name, value))

    def __getattr__(self, name: str) -> "Proxy":
        self.__dict__['__ops'].append(GetAttr(name))
        return self

    def __setitem__(self, key: typing.Any, value: typing.Any) -> None:
        self.__dict__['__ops'].append(SetItem(key, value))

    def __getitem__(self, key: typing.Any) -> "Proxy":
        self.__dict__['__ops'].append(GetItem(key))
        return self

    def __call__(self, *args: typing.Any, **kwargs: typing.Any) -> None:
        self.__dict__['__ops'].append(Call(args, kwargs))


def _get_ops(proxy: Proxy) -> typing.List[PropertyOp]:
    return proxy.__dict__['__ops']  # type:ignore


def build_model(initial_value: StateT) -> protocols.Model[StateT]:
    return Model(initial_value)


class _SubModel(typing.Generic[RootStateT, StateT], protocols.Model[StateT]):
    def __init__(self, parent: protocols.Model[RootStateT],
                 root_property: typing.Callable[[RootStateT], StateT]):
        self.__parent = parent
        self.__root_property = root_property

    def observe(self, callback: EventCallback[StateT]) -> EventToken:
        return self.__parent.observe_property(self.__root_property, callback)

    def observe_property(self, property: typing.Callable[[StateT], T],
                         callback: EventCallback[T]) -> EventToken:
        def cb(state: RootStateT) -> T:
            return property(self.__root_property(state))

        return self.__parent.observe_property(cb, callback)

    def update_state(self, func: StateUpdateCallback[StateT]) -> None:
        def update(state: RootStateT) -> None:
            return func(self.__root_property(state))

        return self.__parent.update_state(update)

    def update_state_root(self, root: typing.Callable[[StateT], T],
                          func: StateUpdateCallback[T]) -> None:
        def update_state_root(state: RootStateT) -> T:
            return root(self.__root_property(state))

        self.__parent.update_state_root(update_state_root, func)

    def update_properties(self, **kwargs: typing.Any) -> None:
        def update_properties(state: RootStateT) -> None:
            for key, value in kwargs.items():
                setattr(self.__root_property(state), key, value)

        self.__parent.update_state(update_properties)

    @property
    def state(self) -> StateT:
        return self.__root_property(self.__parent.state)

    def wait_for(self) -> Event[StateT]:
        return self.__parent.wait_for_property(self.__root_property)

    def wait_for_property(self, property: typing.Callable[[StateT], T]) -> Event[T]:
        def wait_for_property(state: RootStateT) -> T:
            return property(self.__root_property(state))

        return self.__parent.wait_for_property(wait_for_property)

    def snapshot(self) -> StateT:
        return self.__parent.snapshot_property(self.__root_property)

    def snapshot_property(self, property: typing.Callable[[StateT], T]) -> T:
        def snapshot_property(state: RootStateT) -> T:
            return property(self.__root_property(state))

        return self.__parent.snapshot_property(snapshot_property)

    def restore(self, snapshot: StateT) -> None:
        self.__parent.restore_property(snapshot, self.__root_property)

    def restore_property(self, snapshot: T, property: typing.Callable[[StateT], T]) -> None:
        def restore_property(state: RootStateT) -> T:
            return property(self.__root_property(state))

        self.__parent.restore_property(snapshot, restore_property)

    def submodel(self, property: typing.Callable[[StateT], T]) -> protocols.Model[T]:
        return _SubModel(self, property)
