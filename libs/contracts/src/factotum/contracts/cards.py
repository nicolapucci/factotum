"""Structured content for devices that have a screen."""

from pydantic import AnyHttpUrl, Field

from factotum.contracts.base import Contract


class CardItem(Contract):
    label: str = Field(min_length=1, max_length=100)
    value: str | None = Field(default=None, max_length=200)


class Card(Contract):
    """A titled block of information: a recipe, a list of results, the outcome of an action."""

    title: str = Field(min_length=1, max_length=120)
    body: str | None = Field(default=None, max_length=2000)
    image_url: AnyHttpUrl | None = None
    items: tuple[CardItem, ...] = Field(default=(), max_length=20)
