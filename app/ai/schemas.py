"""Structured outputs expected from Claude."""

from pydantic import BaseModel, Field

from app.db.enums import Condition


class Question(BaseModel):
    field: str = Field(description="Which field is uncertain: model, condition, quantity, included, other")
    question_ru: str = Field(description="Short question to the owner, in Russian")


class Spec(BaseModel):
    name: str = Field(description="Spec name in Russian, e.g. 'цвет', 'размер', 'объём памяти'")
    value: str


class Recognition(BaseModel):
    title_ru: str = Field(description="Short item name for the owner's panel, in Russian, brand/model kept in Latin. E.g. 'Наушники Sony WH-1000XM4'")
    search_query: str = Field(description="Short English query to find comparable listings, e.g. 'sony wh-1000xm4'")
    category: str = Field(description="English category path, e.g. 'electronics/headphones', 'home/furniture/chairs'")
    brand: str | None = None
    model: str | None = None
    specs: list[Spec] = Field(default_factory=list, description="Key specs a buyer cares about (size, color, capacity, material...). Only what is visible or certain.")
    condition: Condition
    condition_notes_ru: str = Field(description="Honest description of visible wear/defects in Russian. Empty string if none visible.")
    included: list[str] = Field(default_factory=list, description="What is visible in the photos besides the item (box, charger, cables, manuals), in Russian")
    quantity: int = Field(description="How many identical units are shown, at least 1")
    quantity_confident: bool
    confidence: float = Field(description="Overall confidence in identification, from 0 to 1")
    questions: list[Question] = Field(default_factory=list, description="Only what genuinely needs the owner's confirmation; max 3")


class PriceProposal(BaseModel):
    new_price_eur: int | None = Field(description="Current official/typical new retail price in EUR, if found")
    new_price_source_url: str | None = None
    relevant_comp_indexes: list[int] = Field(default_factory=list, description="Indexes of comparable listings that really match this item")
    comps_low_eur: int | None = None
    comps_high_eur: int | None = None
    price_eur: int = Field(description="Proposed listing price, a natural round number")
    min_price_eur: int = Field(description="Negotiation floor, shown only to the owner")
    rationale_ru: str = Field(description="One line in Russian, e.g. 'Новая: €120, на Bazaraki: €50–70, состояние отличное → €65, минимум €55'")


class ListingText(BaseModel):
    title: str = Field(description="Title line: what it is + brand/model + key hook")
    body: str = Field(description="Value lines, honest condition, what's included. WITHOUT price, location, delivery or contact lines")


class MasterTexts(BaseModel):
    ru: ListingText
    en: ListingText
