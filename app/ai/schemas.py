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


class ChatRules(BaseModel):
    rules_found: bool = Field(description="True if the description or pinned messages contain posting rules")
    posting_allowed: bool | None = Field(description="Can private sellers post sale ads here? null if unclear")
    max_posts_per_day: int | None = Field(description="Max sale posts per user per day; null if not stated")
    min_interval_hours: int | None = Field(description="Min hours between posts of one user (e.g. 'once in 3 days' = 72); null if not stated")
    same_item_repeat_hours: int | None = Field(description="Min hours before re-posting the same item; null if not stated")
    requires_price: bool = Field(description="Price must be stated in the post")
    required_hashtags: list[str] = Field(description="Hashtags every post must contain, with #")
    allowed_hashtags: list[str] = Field(description="Hashtags/categories the chat offers to choose from, with #")
    max_photos: int | None = Field(description="Max photos per post; null if not stated")
    max_text_length: int | None = Field(description="Max characters per post; null if not stated")
    allowed_days: list[str] = Field(description="Weekdays when sale posts are allowed: mon,tue,wed,thu,fri,sat,sun. Empty = any day")
    format_notes_ru: str = Field(description="Format requirements in Russian (e.g. 'указывать район', 'без ссылок'). Empty if none")
    forbidden_ru: str = Field(description="What is forbidden, in Russian. Empty if nothing relevant")
    summary_ru: str = Field(description="1-2 lines in Russian: the rules that matter for a private seller")
    confidence: float = Field(description="0..1, how sure you are the rules were read correctly")
    questions_ru: list[str] = Field(description="What the owner should clarify, in Russian; max 3")
    sale_topic_id: int | None = Field(default=None, description="Forum chats only: id of the topic where sale ads must be posted (from the topic list); null otherwise")


class ChatVersion(BaseModel):
    title: str = Field(description="Title line")
    body: str = Field(description="Value lines, condition, what's included. WITHOUT price, quantity, location, delivery, contact or hashtags")
    hashtags: list[str] = Field(description="Hashtags for this chat, with #. Empty if the chat uses none")


class EditedPost(BaseModel):
    text: str = Field(description="The complete edited post: title line, blank line, body. Same format as the input")


class InquiryMatch(BaseModel):
    sales_related: bool = Field(description="False for spam, ads, greetings with no buying interest, unrelated chat")
    item_id: int | None = Field(description="id from the item list the buyer is asking about; null if unclear")
    confidence: float = Field(description="0..1 confidence in item_id")
    intent: str = Field(description="One of: question, wants_to_buy, price_offer, reserve, other")
    price_offer_eur: int | None = Field(description="Price the buyer offers, if any")
    summary_ru: str = Field(description="What the buyer wants, in Russian, max 100 characters")


class MarketplaceVerdict(BaseModel):
    id: int = Field(description="The number in square brackets")
    is_marketplace: bool = Field(description="True for buy/sell/flea-market/giveaway chats where private people post items for sale")
    reason_ru: str = Field(description="2-6 words in Russian, e.g. 'барахолка Лимасола', 'новости, не барахолка'")


class MarketplaceList(BaseModel):
    verdicts: list[MarketplaceVerdict]
