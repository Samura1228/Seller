from enum import StrEnum


class BatchStatus(StrEnum):
    RECEIVING = "receiving"
    UNPACKING = "unpacking"
    PROCESSING = "processing"
    DONE = "done"
    FAILED = "failed"


class ItemStatus(StrEnum):
    NEW = "new"                          # photos imported, AI not finished yet
    RECOGNIZED = "recognized"            # AI recognition done, price/text in progress
    PENDING_APPROVAL = "pending_approval"
    READY = "ready"
    PUBLISHED = "published"
    HAS_INQUIRIES = "has_inquiries"
    RESERVED = "reserved"
    SOLD = "sold"
    WITHDRAWN = "withdrawn"


class Condition(StrEnum):
    NEW_SEALED = "new_sealed"
    LIKE_NEW = "like_new"
    GOOD = "good"
    USED = "used"


class TextPlatform(StrEnum):
    TELEGRAM_MASTER = "telegram_master"
    BAZARAKI = "bazaraki"
    KYMA = "kyma"
    FACEBOOK = "facebook"


class Language(StrEnum):
    RU = "ru"
    EN = "en"


class TextStatus(StrEnum):
    DRAFT = "draft"
    APPROVED = "approved"


class PriceReason(StrEnum):
    INITIAL = "initial"
    MANUAL = "manual"
    WEEKLY_DROP = "weekly_drop"


class Actor(StrEnum):
    AI = "ai"
    ME = "me"
    SYSTEM = "system"


class ApprovalKind(StrEnum):
    ITEM_REVIEW = "item_review"          # recognition + price + master texts in one card (stage 1)
    PRICE_DROP = "price_drop"            # stage 6
    CHAT_PREVIEWS = "chat_previews"      # stage 2
    CHAT_RULES = "chat_rules"            # stage 2


class ApprovalStatus(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    SKIPPED = "skipped"


# ---- stages 2+ ----

class ChatLanguage(StrEnum):
    RU = "ru"
    EN = "en"
    MIXED = "mixed"


class RulesStatus(StrEnum):
    AUTO = "auto"
    NEEDS_ME = "needs_me"
    CONFIRMED = "confirmed"


class PublicationStatus(StrEnum):
    DRAFT = "draft"
    APPROVED = "approved"
    QUEUED = "queued"
    POSTED = "posted"
    FAILED = "failed"
    DELETED = "deleted"                  # we deleted it (sold / withdrawn)
    MARKED_SOLD = "marked_sold"
    REMOVED_BY_CHAT = "removed_by_chat"  # admins / a bot of the chat deleted it


class InquirySource(StrEnum):
    DM = "dm"
    REPLY = "reply"
    FORWARD = "forward"


class MatchMethod(StrEnum):
    REPLY = "reply"
    FORWARD = "forward"
    AI_TEXT = "ai_text"
    MANUAL = "manual"


class InquiryStatus(StrEnum):
    NEW = "new"
    UNIDENTIFIED = "unidentified"
    HANDLED = "handled"


class ReservationStatus(StrEnum):
    ACTIVE = "active"
    RELEASED = "released"
    CONVERTED = "converted"


class ExternalPlatform(StrEnum):
    BAZARAKI = "bazaraki"
    KYMA = "kyma"
    FACEBOOK = "facebook"


class ExternalStatus(StrEnum):
    WAITING = "waiting"                # in the queue for the helper
    FILLING = "filling"                # the helper took it and is filling the form
    FILLED = "filled"                  # form filled, screenshot sent, waiting for my decision
    APPROVED = "approved"              # I said "publish", the helper presses the button
    PUBLISHED = "published"
    FAILED = "failed"                  # gave up after several attempts
    CANCELLED = "cancelled"            # I don't want it on this platform / item gone before publishing
    REMOVAL_NEEDED = "removal_needed"  # sold or withdrawn: the helper must delete the ad
    REMOVING = "removing"
    REMOVED = "removed"
