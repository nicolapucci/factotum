"""Which of the languages the assistant has words for a short message is in (ADR 0028).

When no skill fits a message nobody has read it: no model was asked (the message is far from every
skill, or the one that reads it was away), and the sentence that says so has to be in the language
the person spoke. A statistical detector tells nothing of "ciao" or "tell me a joke" (measured: it
names Portuguese, Finnish, Turkish), so this one counts the small words of each language, which a
short message is made of: more of one than of the others, and that is its language; a tie, or none,
and it is nobody's (the caller answers in the language it was told to).

It is offline, has no model and gives the same answer every time. A language the assistant learns
words for is one more entry in ``_WORDS``: the tests say that every language of a catalog is here.
A word that two languages share (``a``, ``in``, ``no``, ``stop``, ``router``) is in neither.
"""

import re


def _words(text: str) -> frozenset[str]:
    return frozenset(text.split())


_WORDS: dict[str, frozenset[str]] = {
    "it": _words(
        """
        il lo la le gli un una uno di del dello della dei degli delle e è sono sei siamo siete ho
        hai ha abbiamo hanno che chi cosa come dove quando quanto quanta quanti quante perché
        perche per con su da non mi ti ci vi si ma o anche più molto al alla alle allo agli dal
        dalla nel nella nei nelle sul sulla sui questo questa questi queste quello quella mio mia
        miei tuo tua suo sua nostro dimmi dammi fammi accendi spegni apri chiudi alza abbassa metti
        togli ciao grazie
        prego buongiorno buonasera buonanotte sì ore ora oggi domani ieri adesso fa tempo luce luci
        forno musica casa qual quale quali ancora già fra tra se mai sempre tutto tutti niente nulla
        stanza cucina salotto porta finestra
        """
    ),
    "en": _words(
        """
        the an is are was were am be been being what who whom whose how where when why which do does
        did can could would should will shall please my me you your it its this that these those of
        to for with and or not turn switch open close set tell play start hello hi thanks thank yes
        weather time today tomorrow yesterday light lights oven music kitchen living room bedroom
        door window there here have has had we they he she him her them our their his make get put
        take give show say said want need let just very more most some any know think like good
        """
    ),
}

LANGUAGES = frozenset(_WORDS)
"""The languages it tells apart."""

_WORD = re.compile(r"[^\W\d_]+")
_ELISION = re.compile(r"\b(?:l|un|dell|all|nell|sull|dall|c|com|quest|quell|d|n)'\w")
_ACCENTED = re.compile(r"[àèéìòù]")
_CONTRACTION = re.compile(r"[a-z]'(?:s|t|re|ve|ll|d|m)\b")


def detect_language(text: str) -> str | None:
    """The language that ``text`` is in, among ``LANGUAGES``, or nothing when it cannot be told."""
    folded = text.casefold()
    scores = dict.fromkeys(_WORDS, 0)
    for word in _WORD.findall(folded):
        for language, known in _WORDS.items():
            if word in known:
                scores[language] += 1
    # Italian is written with elisions (``l'aria``, ``c'è``) and accents that English does not have,
    # and English with contractions (``what's``, ``don't``) that Italian does not.
    scores["it"] += len(_ELISION.findall(folded)) + (1 if _ACCENTED.search(folded) else 0)
    scores["en"] += len(_CONTRACTION.findall(folded))
    ranked = sorted(scores.items(), key=lambda item: item[1], reverse=True)
    (best, top), (_, next_best) = ranked[0], ranked[1]
    return best if top > next_best else None
