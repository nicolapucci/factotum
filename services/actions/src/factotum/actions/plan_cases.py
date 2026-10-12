"""Messages, and what a model that reads them well does with each: the corpus that the live test
measures a model against (``tests/live``), and that a test of its own keeps honest.

The messages are said to the devices of ``config/devices.example.yaml`` about the skills in
``skills/examples``, in English and in Italian. Some ask for something to be done, and what becomes
of each depends on where it is said (the shared speaker may not open the garage door, the display
may, once it is confirmed); more are questions, some of them worded so as to look like commands. A
model is asked to be careful: what it must never do is take a question for a command, because that
is the one mistake that does something nobody asked for.
"""

from dataclasses import dataclass, field

from factotum.contracts.actions import ActStatus
from factotum.contracts.skills import ParamValue

KITCHEN = "Kitchen speaker"
"""Shared, and it may start what can be undone."""

DISPLAY = "Living room display"
"""Shared, and its administrator lets it start what cannot be undone, once that is confirmed."""

PHONE = "My phone"
"""The development user's own, and the only place where both lamps are theirs to switch."""


@dataclass(frozen=True, slots=True)
class PlanCase:
    message: str
    speaker: str
    """The device it is said to."""

    skill: str | None
    """The skill it asks for, or nothing when no skill fits it (a question, chit-chat)."""

    status: ActStatus
    """What becomes of it. ``NO_SKILL`` for what no skill fits."""

    device: str | None = None
    """The device it is for, when it is for one that is not the one it is said to."""

    params: dict[str, ParamValue] = field(default_factory=dict)
    """The values that must be understood in it, and no others are looked at."""


def _action(
    said: str,
    speaker: str,
    skill: str,
    status: ActStatus,
    device: str | None = None,
    /,
    **params: ParamValue,
) -> PlanCase:
    """What is said, where, what it asks for and what comes of it; the rest are the values in it,
    which may be called ``message`` as a skill's are."""
    return PlanCase(said, speaker, skill, status, device, params)


def _question(message: str, speaker: str = KITCHEN) -> PlanCase:
    return PlanCase(message, speaker, None, ActStatus.NO_SKILL)


_DONE = ActStatus.DONE
_ASKS = ActStatus.NEEDS_CONFIRMATION
_MORE = ActStatus.NEEDS_MORE
_REFUSED = ActStatus.REFUSED

PLAN_CASES: tuple[PlanCase, ...] = (
    # ---- a lamp: what can be undone is done at once, on the lamp the person may switch
    _action("turn on the living room lamp", KITCHEN, "lamp", _DONE, "Living room lamp", state="on"),
    _action("switch off the lamp", KITCHEN, "lamp", _DONE, "Living room lamp", state="off"),
    _action(
        "set the lamp to 40 percent",
        KITCHEN,
        "lamp",
        _DONE,
        "Living room lamp",
        state="on",
        brightness=40,
    ),
    _action(
        "could you please turn the light on", KITCHEN, "lamp", _DONE, "Living room lamp", state="on"
    ),
    _action("accendi la luce del salotto", KITCHEN, "lamp", _DONE, "Living room lamp", state="on"),
    _action("spegni la lampada", KITCHEN, "lamp", _DONE, "Living room lamp", state="off"),
    _action(
        "metti la luce al 50 per cento",
        KITCHEN,
        "lamp",
        _DONE,
        "Living room lamp",
        state="on",
        brightness=50,
    ),
    _action("turn on the lamp in the bedroom", PHONE, "lamp", _DONE, "Bedroom lamp", state="on"),
    _action("turn on my lamp", PHONE, "lamp", _DONE, "Bedroom lamp", state="on"),
    _action("spegni la luce della camera", PHONE, "lamp", _DONE, "Bedroom lamp", state="off"),
    _action(
        "could you dim the lights to thirty percent",
        KITCHEN,
        "lamp",
        _DONE,
        "Living room lamp",
        state="on",
        brightness=30,
    ),
    _action(
        "can you switch the lamp off?", KITCHEN, "lamp", _DONE, "Living room lamp", state="off"
    ),
    _action(
        "accendimi la lampada, per favore", KITCHEN, "lamp", _DONE, "Living room lamp", state="on"
    ),
    # ---- the garage door: not from the shared speaker, and asked about first from the others
    _action("open the garage door", KITCHEN, "garage-door", _REFUSED, "Garage door", action="open"),
    _action("open the garage door", DISPLAY, "garage-door", _ASKS, "Garage door", action="open"),
    _action("close the garage", DISPLAY, "garage-door", _ASKS, "Garage door", action="close"),
    _action("apri il garage", DISPLAY, "garage-door", _ASKS, "Garage door", action="open"),
    _action(
        "chiudi la porta del garage", PHONE, "garage-door", _ASKS, "Garage door", action="close"
    ),
    _action(
        "I want the garage door shut", DISPLAY, "garage-door", _ASKS, "Garage door", action="close"
    ),
    _action(
        "could you open up the garage for me",
        PHONE,
        "garage-door",
        _ASKS,
        "Garage door",
        action="open",
    ),
    # ---- the volume of the device that is spoken to
    _action("set the volume to 30", KITCHEN, "volume", _DONE, level=30),
    _action("volume at fifty percent", KITCHEN, "volume", _DONE, level=50),
    _action("alza il volume a 70", KITCHEN, "volume", _DONE, level=70),
    _action("abbassa il volume a 20", DISPLAY, "volume", _DONE, level=20),
    _action("bump the volume up to 80", KITCHEN, "volume", _DONE, level=80),
    _action("metti il volume a 40", DISPLAY, "volume", _DONE, level=40),
    # ---- what is missing is asked for, never made up
    _action("turn it up", KITCHEN, "volume", _MORE),
    _action("make the volume a bit lower", KITCHEN, "volume", _MORE),
    _action("what is the weather like", KITCHEN, "weather", _MORE),
    # ---- the weather: it only reads, so nothing is asked
    _action("what is the weather like in Rome", KITCHEN, "weather", _DONE, place="Rome"),
    _action("is it raining in Milan right now", KITCHEN, "weather", _DONE, place="Milan"),
    _action("che tempo fa a Torino", KITCHEN, "weather", _DONE, place="Torino"),
    _action("what's the weather in Naples today", DISPLAY, "weather", _DONE, place="Naples"),
    _action("is it going to rain in Genoa?", KITCHEN, "weather", _DONE, place="Genoa"),
    _action("che tempo c'è a Bologna oggi?", DISPLAY, "weather", _DONE, place="Bologna"),
    # ---- the air: it only reads too, and it is asked for a place like the weather
    _action("how is the air quality in Rome", KITCHEN, "air-quality", _DONE, place="Rome"),
    _action("is the air clean in Milan", DISPLAY, "air-quality", _DONE, place="Milan"),
    _action("what is the air like in Turin today", KITCHEN, "air-quality", _DONE, place="Turin"),
    _action("com'è la qualità dell'aria a Torino", KITCHEN, "air-quality", _DONE, place="Torino"),
    _action("com'è l'aria a Milano", DISPLAY, "air-quality", _DONE, place="Milano"),
    _action("how is the air quality", KITCHEN, "air-quality", _MORE),
    # ---- an announcement: said on the device that is spoken to, or on the one that is named
    _action(
        "tell everyone that dinner is ready", KITCHEN, "announce", _DONE, message="dinner is ready"
    ),
    _action(
        "announce that the guests have arrived",
        DISPLAY,
        "announce",
        _DONE,
        message="the guests have arrived",
    ),
    _action("say that it is time to go", KITCHEN, "announce", _DONE, message="it is time to go"),
    _action(
        "announce on the living room display that dinner is ready",
        KITCHEN,
        "announce",
        _DONE,
        "Living room display",
        message="dinner is ready",
    ),
    _action(
        "annuncia che la cena è pronta", KITCHEN, "announce", _DONE, message="la cena è pronta"
    ),
    _action("dì che è ora di andare", DISPLAY, "announce", _DONE, message="è ora di andare"),
    # ---- the scene for a movie: lights, by Home Assistant, and it can be undone
    _action("set the movie scene", KITCHEN, "movie-scene", _DONE),
    _action("it is movie time", DISPLAY, "movie-scene", _DONE),
    _action("prepara la scena per il film", KITCHEN, "movie-scene", _DONE),
    _action("abbassa le luci, guardiamo un film", DISPLAY, "movie-scene", _DONE),
    # ---- the router: somebody has to approve it, and nobody can yet
    _action("restart the router", KITCHEN, "restart-router", _REFUSED),
    _action("riavvia il router", DISPLAY, "restart-router", _REFUSED),
    _action("reboot the wifi", PHONE, "restart-router", _REFUSED),
    _action("please power cycle the router", KITCHEN, "restart-router", _REFUSED),
    # ---- questions, some of them dressed as commands
    _question("what is the capital of France?"),
    _question("how do I reset the router to factory settings?"),
    _question("why does the garage door make a grinding noise?"),
    _question("what does the manual say about the warranty?"),
    _question("tell me a joke"),
    _question("how does a dimmer switch work?"),
    _question("quanto è alto il monte bianco?"),
    _question("come si cambia la lampadina di una lampada da tavolo?"),
    _question("explain how the volume knob of a radio works"),
    _question("thanks, that is all"),
    _question("che cos'è un router?"),
    _question("should I close the garage door at night?", DISPLAY),
    _question("how do I turn on the lamp with Home Assistant?"),
    _question("what did I ask you about the router yesterday?"),
    _question("what is the weather usually like in Rome in August?"),
    _question("perché il router si riavvia da solo?"),
    _question("ok", PHONE),
    _question("quale lampada consumerebbe meno, una led o una alogena?"),
    _question("is it dangerous to leave a lamp on all night?"),
    _question("what volume is safe for headphones?"),
    _question("can you tell me what a router restart actually does?"),
    _question("how much does a garage door opener cost?"),
    _question("how is the air quality index worked out?"),
    _question("is polluted air bad for asthma?"),
    _question("how do I set up a movie scene in Home Assistant?"),
    _question("what is a good speaker for announcements?"),
    _question("perché l'aria di città è così inquinata?"),
)
