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
    # ---- the volume of the device that is spoken to, set to a level that is said
    _action("set the volume to 30", KITCHEN, "volume", _DONE, level=30),
    _action("volume at fifty percent", KITCHEN, "volume", _DONE, level=50),
    _action("alza il volume a 70", KITCHEN, "volume", _DONE, level=70),
    _action("abbassa il volume a 20", DISPLAY, "volume", _DONE, level=20),
    _action("bump the volume up to 80", KITCHEN, "volume", _DONE, level=80),
    _action("metti il volume a 40", DISPLAY, "volume", _DONE, level=40),
    # ---- the volume turned up or down: by what is said, and by the skill's step when it is not
    _action("turn the volume up", KITCHEN, "volume-change", _DONE, direction="up"),
    _action("turn it up", KITCHEN, "volume-change", _DONE, direction="up"),
    _action("make the volume a bit lower", KITCHEN, "volume-change", _DONE, direction="down"),
    _action(
        "turn the volume down by 20", DISPLAY, "volume-change", _DONE, direction="down", amount=20
    ),
    _action("alza il volume", KITCHEN, "volume-change", _DONE, direction="up"),
    _action(
        "abbassa il volume di quindici",
        DISPLAY,
        "volume-change",
        _DONE,
        direction="down",
        amount=15,
    ),
    # ---- the sound muted or brought back (the device keeps the level)
    _action("mute the sound", KITCHEN, "mute", _DONE, muted=True),
    _action("unmute", DISPLAY, "mute", _DONE, muted=False),
    _action("metti in muto", KITCHEN, "mute", _DONE, muted=True),
    _action("togli il muto", DISPLAY, "mute", _DONE, muted=False),
    # ---- how the device is: asked of the device, and said from what it answered
    _action("what is the volume", KITCHEN, "volume-status", _DONE),
    _action("is it muted", DISPLAY, "volume-status", _DONE),
    _action("che volume c'è", KITCHEN, "volume-status", _DONE),
    # ---- a timer, kept by the device: the words of how long are what the model copies
    _action("set a timer for five minutes", KITCHEN, "timer", _DONE, duration="five minutes"),
    _action("start a ten minute timer", DISPLAY, "timer", _DONE, duration="ten minute"),
    _action(
        "set a timer for an hour and a half", KITCHEN, "timer", _DONE, duration="an hour and a half"
    ),
    _action("metti un timer di cinque minuti", KITCHEN, "timer", _DONE, duration="cinque minuti"),
    _action("imposta un timer di mezz'ora", DISPLAY, "timer", _DONE, duration="mezz'ora"),
    _action("avvisami tra dieci minuti", PHONE, "timer", _DONE, duration="dieci minuti"),
    _action("cancel the timer", KITCHEN, "timer-cancel", _DONE),
    _action("stop the timer", DISPLAY, "timer-cancel", _DONE),
    _action("cancel the five minute timer", KITCHEN, "timer-cancel", _DONE, duration="five minute"),
    _action("annulla il timer", KITCHEN, "timer-cancel", _DONE),
    _action("ferma il timer", DISPLAY, "timer-cancel", _DONE),
    _action(
        "spegni il timer da cinque minuti", KITCHEN, "timer-cancel", _DONE, duration="cinque minuti"
    ),
    _action("how much time is left on the timer", KITCHEN, "timer-status", _DONE),
    _action("are there any timers", DISPLAY, "timer-status", _DONE),
    _action("quanto manca al timer", KITCHEN, "timer-status", _DONE),
    _action("quanto tempo resta", PHONE, "timer-status", _DONE),
    # ---- an alarm, kept by the device: the hour and the day, in the words of the person
    _action("set an alarm for seven thirty", KITCHEN, "alarm", _DONE, time="seven thirty"),
    _action("wake me up at 6 tomorrow", DISPLAY, "alarm", _DONE, time="6", day="tomorrow"),
    _action(
        "set an alarm for monday at eight", KITCHEN, "alarm", _DONE, time="eight", day="monday"
    ),
    _action("metti la sveglia alle sette e mezza", KITCHEN, "alarm", _DONE, time="sette e mezza"),
    _action("svegliami domani alle 6", DISPLAY, "alarm", _DONE, time="6", day="domani"),
    _action(
        "imposta una sveglia per lunedì alle otto",
        KITCHEN,
        "alarm",
        _DONE,
        time="otto",
        day="lunedì",
    ),
    _action("cancel the alarm", KITCHEN, "alarm-cancel", _DONE),
    _action("stop the alarm", DISPLAY, "alarm-cancel", _DONE),
    _action("cancel the seven thirty alarm", KITCHEN, "alarm-cancel", _DONE, time="seven thirty"),
    _action("cancella la sveglia", KITCHEN, "alarm-cancel", _DONE),
    _action("spegni la sveglia", DISPLAY, "alarm-cancel", _DONE),
    _action("annulla la sveglia delle sette", KITCHEN, "alarm-cancel", _DONE, time="sette"),
    _action("what alarms do I have", KITCHEN, "alarm-status", _DONE),
    _action("when is my alarm", DISPLAY, "alarm-status", _DONE),
    _action("a che ora suona la sveglia", KITCHEN, "alarm-status", _DONE),
    _action("quali sveglie ho", PHONE, "alarm-status", _DONE),
    # ---- an alarm put off: how much later is what the person said, and the device's snooze if none
    _action("snooze the alarm", KITCHEN, "alarm-postpone", _DONE),
    _action("put the alarm off by ten minutes", DISPLAY, "alarm-postpone", _DONE, by="ten minutes"),
    _action("five more minutes", KITCHEN, "alarm-postpone", _DONE, by="five minutes"),
    _action(
        "postpone the alarm by half an hour", PHONE, "alarm-postpone", _DONE, by="half an hour"
    ),
    _action("rimanda la sveglia", KITCHEN, "alarm-postpone", _DONE),
    _action(
        "rimanda la sveglia di dieci minuti", DISPLAY, "alarm-postpone", _DONE, by="dieci minuti"
    ),
    _action("ancora cinque minuti", KITCHEN, "alarm-postpone", _DONE, by="cinque minuti"),
    _action("posticipa la sveglia di mezz'ora", KITCHEN, "alarm-postpone", _DONE, by="mezz'ora"),
    # ---- an alarm moved to another hour: the new hour and day, and which alarm if it is named
    _action("move the alarm to eight", KITCHEN, "alarm-change", _DONE, time="eight"),
    _action(
        "change the alarm to seven thirty", DISPLAY, "alarm-change", _DONE, time="seven thirty"
    ),
    _action(
        "move the seven o'clock alarm to eight",
        KITCHEN,
        "alarm-change",
        _DONE,
        time="eight",
        **{"from": "seven"},
    ),
    _action("sposta la sveglia alle otto", KITCHEN, "alarm-change", _DONE, time="otto"),
    _action(
        "cambia la sveglia alle sette e mezza",
        DISPLAY,
        "alarm-change",
        _DONE,
        time="sette e mezza",
    ),
    _action(
        "sposta la sveglia delle sette alle otto",
        KITCHEN,
        "alarm-change",
        _DONE,
        time="otto",
        **{"from": "sette"},
    ),
    _action(
        "sposta la sveglia a domani alle sei",
        DISPLAY,
        "alarm-change",
        _DONE,
        time="sei",
        day="domani",
    ),
    # ---- enough: whatever rings is stopped, and the person does not say what it is
    _action("stop", KITCHEN, "stop", _DONE),
    _action("that is enough", DISPLAY, "stop", _DONE),
    _action("be quiet", PHONE, "stop", _DONE),
    _action("enough", KITCHEN, "stop", _DONE),
    _action("basta", KITCHEN, "stop", _DONE),
    _action("basta così", DISPLAY, "stop", _DONE),
    _action("ferma", KITCHEN, "stop", _DONE),
    _action("zitto", PHONE, "stop", _DONE),
    # ---- what is missing is asked for, never made up
    _action("move the alarm", KITCHEN, "alarm-change", _MORE),
    _action("sposta la sveglia", DISPLAY, "alarm-change", _MORE),
    _action("set a timer", KITCHEN, "timer", _MORE),
    _action("metti un timer", DISPLAY, "timer", _MORE),
    _action("set an alarm", KITCHEN, "alarm", _MORE),
    _action("metti la sveglia", DISPLAY, "alarm", _MORE),
    _action("change the volume", KITCHEN, "volume-change", _MORE),
    _action("cambia il volume", DISPLAY, "volume-change", _MORE),
    _action("set the volume", KITCHEN, "volume", _MORE),
    _action("what is the weather like", KITCHEN, "weather", _MORE),
    _action("che tempo fa", DISPLAY, "weather", _MORE),
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
    _question("how does a kitchen timer work?"),
    _question("how long should I boil an egg?"),
    _question("who invented the alarm clock?"),
    _question("why does the alarm of my phone not ring when it is on silent?"),
    _question("perché il timer del forno si ferma da solo?"),
    _question("quanti minuti ci sono in un giorno?"),
    _question("is it safe to leave a timer running overnight?"),
    _question("what is a good time to wake up?"),
    _question("how loud is a smoke alarm?"),
    _question("quanto è lungo un minuto di silenzio?"),
    _question("what is a good speaker for announcements?"),
    _question("perché l'aria di città è così inquinata?"),
    _question("how long is a snooze usually?"),
    _question("is it bad to snooze the alarm every morning?"),
    _question("what does the snooze button of an alarm clock do?"),
    _question("perché la sveglia del telefono suona sempre in ritardo?"),
    _question("quanto dura di solito lo snooze?"),
    _question("how do I move a meeting to eight?"),
    _question("how do I stop my dog from barking?"),
    _question("come si ferma una perdita d'acqua?"),
    _question("is it enough to boil the water for five minutes?"),
)
