import copy
import logging
import traceback
import typing
from collections import defaultdict
from dataclasses import dataclass, field, is_dataclass
from enum import Enum

from soso.state.event import Event, EventCallback, EventToken

__all__ = ['Model', 'StateT', 'T', 'PropertyCallback']

StateT_contra = typing.TypeVar('StateT_contra', contravariant=True)
StateT = typing.TypeVar('StateT')
T = typing.TypeVar('T')
T_contra = typing.TypeVar('T_contra', contravariant=True)
T_co = typing.TypeVar('T_co', covariant=True)


class PropertyCallback(typing.Generic[StateT_contra, T_co], typing.Protocol):
    def __call__(self, __state: StateT_contra) -> T_co:
        ...


class StateUpdateCallback(typing.Generic[StateT_contra], typing.Protocol):
    def __call__(self, __state: StateT_contra) -> None:
        ...


class AttributeAccess(Enum):
    SETATTR = 1
    GETATTR = 2
    SETITEM = 3
    GETITEM = 4


@dataclass
class PropertyOp:
    access: AttributeAccess
    key: typing.Any
    value: typing.Optional[typing.Any] = None

    def execute(
            self, obj: typing.Any
    ) -> typing.Tuple[typing.Optional[typing.Any], bool]:
        if self.access == AttributeAccess.GETATTR:
            return getattr(obj, self.key), False
        elif self.access == AttributeAccess.GETITEM:
            return obj[self.key], False
        elif self.access == AttributeAccess.SETATTR:
            curr_value = getattr(obj, self.key)
            changed = curr_value != self.value
            if changed:
                setattr(obj, self.key, self.value)
            return None, changed
        else:
            assert self.access == AttributeAccess.SETITEM
            try:
                curr_value = obj[self.key]
                changed = curr_value != self.value
            except KeyError:
                changed = True
            if changed:
                obj[self.key] = self.value
            return None, changed

    def get_value(self, obj: typing.Any) -> typing.Any:
        if self.access in [AttributeAccess.GETATTR, AttributeAccess.SETATTR]:
            return getattr(obj, self.key)
        else:
            assert self.access in [
                AttributeAccess.GETITEM, AttributeAccess.SETITEM
            ]
            return obj[self.key]


@dataclass
class Node:
    children: typing.DefaultDict[str, "Node"] = field(
        default_factory=lambda: defaultdict(Node))
    event: Event[typing.Any] = field(
        default_factory=lambda: Event("NodeUpdateEvent"))
    # The type of access to this node
    op: typing.Optional[PropertyOp] = None


class Model(typing.Generic[StateT]):
    def __init__(self) -> None:
        model_klass = self.__orig_bases__[-1]  # type:ignore
        self.__state_klass = state_klass = typing.get_args(model_klass)[0]
        assert is_dataclass(state_klass)
        self.__current_state: StateT = state_klass()
        self.__root = Node()

    def __get_node_for_ops(self, ops: typing.List[PropertyOp]) -> Node:
        root: Node = self.__root
        children = []
        for op in ops:
            child = op.key
            children.append(str(child))
            root = root.children[child]
            root.op = op
        root.event._name = ".".join(children)
        return root

    def __get_value_for_ops(self, ops: typing.List[PropertyOp]) -> typing.Any:
        root: typing.Any = self.__current_state
        for op in ops:
            root = op.get_value(root)
        return root

    def observe(self, func: PropertyCallback[StateT, T],
                callback: EventCallback[T]) -> EventToken:
        event, ops = self.__event(func)
        token = event.connect(callback)
        value = self.__get_value_for_ops(ops)
        # call with the initial value
        callback(value)
        return token

    def __event(
        self, func: PropertyCallback[StateT, T]
    ) -> typing.Tuple[Event[T], typing.List[PropertyOp]]:
        proxy: StateT = typing.cast(StateT, Proxy())
        func(proxy)
        ops = _get_ops(proxy)  # type:ignore

        node = self.__get_node_for_ops(ops)
        return node.event, ops

    def event_trapdoor(self, property: PropertyCallback[StateT,
                                                        T]) -> Event[T]:
        return self.__event(property)[0]

    async def wait_for(self, property: PropertyCallback[StateT, T]) -> T:
        result = await self.event_trapdoor(property)
        return result  # type:ignore

    def snapshot(self,
                 property: PropertyCallback[StateT, T] = lambda x: x) -> T:
        subtree = property(self.state)
        return copy.deepcopy(subtree)

    def restore(
            self,
            snapshot: typing.Union[T, StateT],
            property: typing.Optional[PropertyCallback[StateT,
                                                       T]] = None) -> None:
        if property is None:
            self.__current_state = typing.cast(StateT, snapshot)
            self.__fire_all_child_events(self.__root, self.__current_state)
        else:
            proxy = typing.cast(StateT, Proxy())
            property(proxy)
            ops = _get_ops(typing.cast(Proxy, proxy))
            node = self.__get_node_for_ops(ops)
            if ops[-1].access == AttributeAccess.GETATTR:
                ops[-1].access = AttributeAccess.SETATTR
            elif ops[-1].access == AttributeAccess.GETITEM:
                ops[-1].access = AttributeAccess.SETITEM
            else:
                assert False
            ops[-1].value = snapshot
            obj: typing.Optional[typing.Any] = self.__current_state
            for op in ops:
                obj, _ = op.execute(obj)
            assert obj is None  # Last should have been a set
            value = property(self.__current_state)
            node.event.emit(value)
            self.__fire_all_child_events(node, value)

    def __fire_all_child_events(self, node: Node, parent: typing.Any) -> None:
        for name, child_node in node.children.items():
            try:
                assert child_node.op is not None
                child_value = child_node.op.get_value(parent)
                child_node.event.emit(child_value)
                self.__fire_all_child_events(child_node, child_value)
            except Exception:
                logging.getLogger(__name__).info(traceback.format_exc())

    @typing.overload
    def update(self, **kwargs: typing.Any) -> None:
        ...

    @typing.overload
    def update(self, func: StateUpdateCallback[StateT]) -> None:
        ...

    def update(self, *args: StateUpdateCallback[StateT],
               **kwargs: typing.Any) -> None:
        func: StateUpdateCallback[StateT]

        if len(args) == 0:
            assert len(kwargs) != 0

            def doit(state: StateT) -> None:
                for key, value in kwargs.items():
                    setattr(state, key, value)

            func = doit
        else:
            assert len(args) == 1
            assert len(kwargs) == 0
            assert callable(args[0])
            func = args[0]

        # Get all changes
        proxy = Proxy()
        func(proxy)  # type: ignore
        ops = _get_ops(proxy)
        obj: typing.Optional[typing.Any] = self.__current_state

        # Apply changes tos tate
        stmts: typing.List[typing.List[PropertyOp]] = []
        curr_stmt: typing.List[PropertyOp] = []
        for op in ops:
            curr_stmt.append(op)
            obj, changed = op.execute(obj)
            # end of statement
            if obj is None:
                obj = self.__current_state
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

        # Always emit root event
        self.__root.event.emit(self.__current_state)
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

    @property
    # TODO: this should return a read-only view to avoid accidents
    def state(self) -> StateT:
        return self.__current_state


class Proxy:
    def __init__(self) -> None:
        self.__dict__['__ops'] = []

    def __setattr__(self, name: str, value: typing.Any) -> None:
        self.__dict__['__ops'].append(
            PropertyOp(AttributeAccess.SETATTR, name, value))

    def __getattr__(self, name: str) -> "Proxy":
        self.__dict__['__ops'].append(PropertyOp(AttributeAccess.GETATTR,
                                                 name))
        return self

    def __setitem__(self, key: typing.Any, value: typing.Any) -> None:
        self.__dict__['__ops'].append(
            PropertyOp(AttributeAccess.SETITEM, key, value))

    def __getitem__(self, key: typing.Any) -> "Proxy":
        self.__dict__['__ops'].append(PropertyOp(AttributeAccess.GETITEM, key))
        return self


def _get_ops(proxy: Proxy) -> typing.List[PropertyOp]:
    return proxy.__dict__['__ops']  # type:ignore
