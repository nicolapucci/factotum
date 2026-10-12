"""Whether a message says a value (ADR 0028): the check that a value a model wrote can be traced
back to the words of the person.

A model reads the values of a skill out of what was said, and a small one fills in what was not: a
level of 100 for "turn it up", a city for "what is the weather like". The checks on the type and the
bounds cannot tell, because a made-up 100 is a whole number between 0 and 100. This one can, for the
two kinds of value that words give:

* **a number** is one that the message gives, in digits (``30``, ``30%``, ``0,5``) or in words, in
  the two languages the assistant has sentences in (``thirty``, ``twenty-five``, ``trenta``,
  ``venticinque``, up to 999), or an extreme of the range that the skill declares for it, said in
  the common way (``al massimo``, ``at full``, ``a metà``): those have no digit in them, and what
  they mean is the bound of the parameter, not a number that a model chose;
* **a text** is made of words that the message has (so a city, or a thing to announce, is what the
  person said and not what a model thought of).

A choice from a closed list (``on`` or ``off``) and a yes or no cannot be traced: the model reads
"accendi" as ``on`` and no rule in code does. They are left to the checks of the choices, and to the
person's yes where the skill asks for one.

A value that is not said is *not said*: if it was optional the skill is called without it and
applies its own default, and if it was required the person is asked. What is permitted here is on
purpose wide (a word that is also something else, ``sei``, is a number too): the check can only let
through a value that the model wrote, never invent one, so a number too many in the message costs
nothing and a number too few asks a person for what they said.
"""

import re
import unicodedata

from factotum.contracts.skills import ParameterSpec, ParamValue

_DIGITS = re.compile(r"\d+(?:[.,]\d+)?")

_MAXIMUM = frozenset({"max", "maximum", "full", "highest", "massimo", "massima", "pieno", "piena"})
_MINIMUM = frozenset({"min", "minimum", "lowest", "minimo", "minima"})
_HALF = frozenset({"half", "halfway", "meta", "mezzo", "mezza"})
"""How the ends and the middle of a range are said, with the accents taken off (``metà``)."""


def words_of(text: str) -> list[str]:
    """The words of ``text``, in lower case and with the accents taken off, so that a word that was
    heard with the wrong accent is still the word. A hyphen and an apostrophe are word breaks:
    ``twenty-five`` is two words and ``l'aria`` is two."""
    decomposed = unicodedata.normalize("NFKD", text.casefold())
    plain = "".join(letter for letter in decomposed if not unicodedata.combining(letter))
    return re.findall(r"\w+", plain)


# ---- numbers that are written in words -----------------------------------------------------------


def _split(text: str) -> list[str]:
    return text.split()


_EN_ONES = _split(
    "zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen "
    "sixteen seventeen eighteen nineteen"
)
_EN_TENS = _split("_ _ twenty thirty forty fifty sixty seventy eighty ninety")

_IT_ONES = _split(
    "zero uno due tre quattro cinque sei sette otto nove dieci undici dodici tredici quattordici "
    "quindici sedici diciassette diciotto diciannove"
)
_IT_TENS = _split("_ _ venti trenta quaranta cinquanta sessanta settanta ottanta novanta")


def _english(number: int) -> list[tuple[str, ...]]:
    """The ways that ``number`` (0 to 999) is written in English, as words."""
    if number < 20:
        return [(_EN_ONES[number],)]
    if number < 100:
        tens, ones = divmod(number, 10)
        return [(_EN_TENS[tens],)] if ones == 0 else [(_EN_TENS[tens], _EN_ONES[ones])]
    hundreds, rest = divmod(number, 100)
    heads: list[tuple[str, ...]] = [(_EN_ONES[hundreds], "hundred")]
    if hundreds == 1:
        heads.append(("a", "hundred"))
    if rest == 0:
        return heads
    return [
        head + joint + tail for head in heads for tail in _english(rest) for joint in ((), ("and",))
    ]


def _italian(number: int) -> list[str]:
    """The ways that ``number`` (0 to 999) is written in Italian, which writes it as one word: the
    tens lose their last vowel before ``uno`` and ``otto`` (``ventuno``, ``trentotto``), and so does
    ``cento`` before any vowel (``centottanta``, ``centuno``), which is also written whole."""
    if number < 20:
        return [_IT_ONES[number]]
    if number < 100:
        tens, ones = divmod(number, 10)
        if ones == 0:
            return [_IT_TENS[tens]]
        stem = _IT_TENS[tens][:-1] if ones in (1, 8) else _IT_TENS[tens]
        return [stem + _IT_ONES[ones]]
    hundreds, rest = divmod(number, 100)
    head = "cento" if hundreds == 1 else _IT_ONES[hundreds] + "cento"
    if rest == 0:
        return [head]
    forms: list[str] = []
    for tail in _italian(rest):
        forms.append(head + tail)
        if tail[0] in "aeiou":  # centuno, centundici, centotto, centottanta
            forms.append(head[:-1] + tail)
    return forms


def _spelled() -> dict[tuple[str, ...], int]:
    table: dict[tuple[str, ...], int] = {}
    for number in range(1000):
        for form in _english(number):
            table[form] = number
        for word in _italian(number):
            table[(word,)] = number
    return table


_SPELLED = _spelled()
_LONGEST = max(len(form) for form in _SPELLED)


def _without_percent(words: list[str]) -> list[str]:
    """``per cento`` is the percent and not a hundred: the number of a percentage is the one before
    it, and a hundred that a model wrote is not said by it."""
    return [
        word
        for index, word in enumerate(words)
        if not (word == "cento" and words[index - 1 : index] == ["per"])
    ]


def numbers_in(text: str) -> frozenset[float]:
    """The numbers that ``text`` gives, in digits and in words."""
    found: set[float] = {float(match.group().replace(",", ".")) for match in _DIGITS.finditer(text)}
    words = _without_percent(words_of(text))
    index = 0
    while index < len(words):
        for length in range(min(_LONGEST, len(words) - index), 0, -1):
            number = _SPELLED.get(tuple(words[index : index + length]))
            if number is not None:
                found.add(float(number))
                index += length
                break
        else:
            index += 1
    return frozenset(found)


# ---- what a message says -------------------------------------------------------------------------


def says(message: str, spec: ParameterSpec, value: ParamValue) -> bool:
    """Whether ``message`` says ``value`` for a parameter that is declared as ``spec``. A choice
    from a list, and a yes or no, are not something a message can be searched for: they are not
    refused."""
    if spec.enum is not None or isinstance(value, bool):
        return True
    if isinstance(value, int | float):
        return _says_a_number(message, spec, value)
    return _says_a_text(message, value)


def _says_a_number(message: str, spec: ParameterSpec, value: float) -> bool:
    if abs(float(value)) in numbers_in(message):
        return True
    said = set(words_of(message))
    if spec.maximum is not None and value == spec.maximum and said & _MAXIMUM:
        return True
    if spec.minimum is not None and value == spec.minimum and said & _MINIMUM:
        return True
    return (
        spec.minimum is not None
        and spec.maximum is not None
        and value == (spec.minimum + spec.maximum) / 2
        and bool(said & _HALF)
    )


def _says_a_text(message: str, value: str) -> bool:
    wanted = words_of(value)
    if not wanted:
        return False
    said = words_of(message)
    if set(wanted) <= set(said):
        return True
    # A script that is written with no spaces between words (Chinese, Japanese, Thai) has one
    # "word" for a sentence: what is asked for is then that the text is inside it.
    return any(unicodedata.category(letter) == "Lo" for letter in value) and " ".join(
        wanted
    ) in " ".join(said)
