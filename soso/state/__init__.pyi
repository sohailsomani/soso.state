import typing

from soso.event import Event, EventToken

StateT = typing.TypeVar('StateT')


class PropertyCallback(typing.Protocol):
    def __call__(self, state: StateT) -> typing.Any:
        ...


class Model(typing.Generic[StateT]):
    def subscribe(
            self, func: PropertyCallback,
            callback: typing.Callable[[typing.Any], typing.Any]) -> EventToken:
        ...

    @typing.overload
    def update(self, **kwargs: typing.Any) -> None:
        ...

    @typing.overload
    def update(self, func: typing.Callable[[StateT], None]) -> None:
        ...

    @property
    def state(self) -> StateT:
        ...

    def event(self, property: PropertyCallback) -> Event:
        ...

    def snapshot(self) -> StateT:
        ...

    def restore(self, snapshot: StateT) -> None:
        ...
