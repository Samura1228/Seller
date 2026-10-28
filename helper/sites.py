"""Everything site-specific lives here, so a change on a site is fixed in one place.

The helper doesn't hard-code form fields: on every step it sends the page's elements to the server and
Claude decides what to fill or click. Here are only the entry points and the words of buttons that must
never be pressed before the owner confirms in the bot.
"""

import re

SITES = {
    "bazaraki": {
        "name": "Bazaraki",
        "start": "https://www.bazaraki.com/",        # the agent clicks "Post ad" from here
        "login": "https://www.bazaraki.com/login/",
    },
    "kyma": {
        "name": "Kyma.cy",
        "start": "https://kyma.cy/",
        "login": "https://kyma.cy/",
    },
    "facebook": {
        "name": "Facebook Marketplace",
        "start": "https://www.facebook.com/marketplace/create/item",
        "login": "https://www.facebook.com/login/",
    },
}

# The final "publish" button: pressing it before the owner's ✅ is refused, whatever the AI says.
# "Next / Continue / Далее" between form steps are allowed.
PUBLISH_WORDS = re.compile(
    r"\b(publish|post( (ad|it|listing|now))?|submit|place (ad|an ad)|list (it|item)|опубликовать|разместить|"
    r"δημοσίευση|δημοσίευσε|καταχώρηση αγγελίας)\b",
    re.I,
)
STEP_WORDS = re.compile(r"\b(next|continue|далее|продолжить|επόμενο|συνέχεια)\b", re.I)


def looks_like_publish(text: str) -> bool:
    text = (text or "").strip()
    return bool(text) and bool(PUBLISH_WORDS.search(text)) and not STEP_WORDS.search(text)
