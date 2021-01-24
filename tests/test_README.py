import asyncio
import typing
import unittest
from dataclasses import dataclass, field
from unittest.mock import MagicMock

from soso import state


@dataclass
class Person:
    first_name: str
    last_name: str


@dataclass
class AppState:
    regional_managers: typing.List[Person] = field(default_factory=list)
    assistant_to_the_regional_managers: typing.List[Person] = field(
        default_factory=list)
    employees: typing.List[Person] = field(default_factory=list)


class AppModel(state.Model[AppState]):
    pass


class TestREADME(unittest.TestCase):
    def test_README(self) -> None:
        app = AppModel()
        app.update(
            regional_managers=[Person("Michael", "Scott")],
            assistant_to_the_regional_managers=[Person("Dwight", "Schrute")],
            employees=[Person("Jim", "Halpert"),
                       Person("Pam", "Beesly")])

        self.assertEqual(app.state.regional_managers,
                         [Person("Michael", "Scott")])
        self.assertEqual(app.state.assistant_to_the_regional_managers,
                         [Person("Dwight", "Schrute")])
        self.assertEqual(app.state.employees,
                         [Person("Jim", "Halpert"),
                          Person("Pam", "Beesly")])

        x: AppState
        mock = MagicMock()
        token = app.subscribe(lambda x: x.regional_managers[0], mock)
        mock.assert_called_with(Person("Michael", "Scott"))

        pams_last_name = MagicMock()
        app.subscribe(lambda x: x.employees[1].last_name, pams_last_name)
        pams_last_name.assert_called_with("Beesly")

        def pam_gets_married(state: AppState) -> None:
            state.employees[1].last_name = "Halpert"

        pams_last_name.reset_mock()
        app.update(pam_gets_married)

        self.assertEqual(app.state.employees[1].last_name, "Halpert")
        pams_last_name.assert_called_with("Halpert")

        mock.reset_mock()
        app.update(regional_managers=[Person("Dwight", "Schrute")],
                   assistant_to_the_regional_managers=[])

        mock.assert_called_with(Person("Dwight", "Schrute"))
        mock.reset_mock()
        token.disconnect()

        task = asyncio.get_event_loop().create_task(self.__myfunc(app))
        asyncio.get_event_loop().call_soon(
            lambda: app.update(regional_managers=[Person("Jim", "Halpert")]))
        asyncio.get_event_loop().run_until_complete(task)

        self.assertEqual(self.__regional_managers, [Person("Jim", "Halpert")])
        # we disconnected the token, so mock shouldn't get called again
        mock.assert_not_called()

    async def __myfunc(self, app: AppModel) -> None:
        regional_managers = await app.event(
            lambda state: state.regional_managers)
        self.__regional_managers = regional_managers
