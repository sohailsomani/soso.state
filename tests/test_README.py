import typing
import unittest
from dataclasses import dataclass, field

from soso import statetree


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


class AppModel(statetree.Model[AppState]):
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

        app.update()