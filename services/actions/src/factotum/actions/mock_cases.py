"""Messages for the mock skills of ``skills/mocks``, and what a model that reads them well does with
each: a second corpus, for skills that the planner was never tuned on.

The corpus of ``plan_cases`` is about the skills that the planner has been shown how to read, and
what is measured with it is partly what it was taught. These skills are written plainly, as the
author of a skill would write them (a description, the values with a word on each), and none of the
examples of the planner is about them; what a model makes of them is how it does with a skill it has
not seen. They are mocks: they do nothing and answer as their ``mock.yaml`` says, because what is
measured is whether the assistant chooses the skill and calls it as its manifest says (the skill,
the values, what it asks when one is missing, what it leaves alone when nobody said it, what it
refuses and what it asks a yes for), and not whether anything is then done.

Several pairs are near each other on purpose: the thermostat and the oven, the blinds set to a
percentage and the blinds opened or closed (as the volume is), the shopping list and the reminder,
the door and the garage, the message and the announcement.
"""

from factotum.actions.plan_cases import DISPLAY, KITCHEN, PHONE, PlanCase
from factotum.contracts.actions import ActStatus
from factotum.contracts.skills import ParamValue

_DONE = ActStatus.DONE
_ASKS = ActStatus.NEEDS_CONFIRMATION
_MORE = ActStatus.NEEDS_MORE
_REFUSED = ActStatus.REFUSED


def _action(
    said: str, speaker: str, skill: str, status: ActStatus, /, **params: ParamValue
) -> PlanCase:
    return PlanCase(said, speaker, skill, status, None, params)


def _question(message: str, speaker: str = KITCHEN) -> PlanCase:
    return PlanCase(message, speaker, None, ActStatus.NO_SKILL)


MOCK_CASES: tuple[PlanCase, ...] = (
    # ---- the heating: a number in a range, and a room if one is said
    _action("set the thermostat to 21 degrees", KITCHEN, "thermostat-set", _DONE, temperature=21),
    _action(
        "set the bedroom thermostat to nineteen",
        DISPLAY,
        "thermostat-set",
        _DONE,
        temperature=19,
        room="bedroom",
    ),
    _action("metti il termostato a 22 gradi", KITCHEN, "thermostat-set", _DONE, temperature=22),
    _action(
        "scalda la camera a 23 gradi",
        PHONE,
        "thermostat-set",
        _DONE,
        temperature=23,
        room="camera",
    ),
    _action(
        "imposta il riscaldamento della cucina a venti gradi",
        KITCHEN,
        "thermostat-set",
        _DONE,
        temperature=20,
        room="cucina",
    ),
    _action("set the thermostat", KITCHEN, "thermostat-set", _MORE),
    _action("metti il termostato a 50 gradi", DISPLAY, "thermostat-set", _MORE, temperature=50),
    # ---- how warm it is: a read, with a room if one is said
    _action("how warm is it at home", KITCHEN, "thermostat-status", _DONE),
    _action(
        "what is the temperature in the kitchen",
        DISPLAY,
        "thermostat-status",
        _DONE,
        room="kitchen",
    ),
    _action("quanti gradi ci sono in casa", KITCHEN, "thermostat-status", _DONE),
    _action("che temperatura c'è in camera", PHONE, "thermostat-status", _DONE, room="camera"),
    # ---- the fan: a choice from a list, which is the model's reading of the words
    _action("put the fan on high", KITCHEN, "fan-speed", _DONE, speed="high"),
    _action("turn the fan to medium", DISPLAY, "fan-speed", _DONE, speed="medium"),
    _action("ventola al minimo", KITCHEN, "fan-speed", _DONE, speed="low"),
    _action("metti la ventola al massimo", PHONE, "fan-speed", _DONE, speed="high"),
    _action("set the fan speed", KITCHEN, "fan-speed", _MORE),
    _action("accendi la ventola", DISPLAY, "fan-speed", _MORE),
    # ---- the blinds to a percentage, and the blinds all the way: two skills, as the volume is
    _action("open the blinds to 40 percent", KITCHEN, "blinds-set", _DONE, position=40),
    _action("tapparelle al 70 per cento", DISPLAY, "blinds-set", _DONE, position=70),
    _action("set the blinds to half", PHONE, "blinds-set", _DONE, position=50),
    _action("apri le tapparelle al venti per cento", KITCHEN, "blinds-set", _DONE, position=20),
    _action("open the blinds", KITCHEN, "blinds-move", _DONE, action="open"),
    _action("chiudi le tapparelle", DISPLAY, "blinds-move", _DONE, action="close"),
    _action("close the blinds please", PHONE, "blinds-move", _DONE, action="close"),
    _action("alza le tapparelle", KITCHEN, "blinds-move", _DONE, action="open"),
    _action("move the blinds", KITCHEN, "blinds-move", _MORE),
    _action("set the blinds to a percentage", DISPLAY, "blinds-set", _MORE),
    # ---- the shopping list: what to add is the words that were said
    _action("add milk to the shopping list", KITCHEN, "shopping-list-add", _DONE, item="milk"),
    _action(
        "aggiungi uova alla lista della spesa", DISPLAY, "shopping-list-add", _DONE, item="uova"
    ),
    _action(
        "put eggs and bread on the list",
        PHONE,
        "shopping-list-add",
        _DONE,
        item="eggs and bread",
    ),
    _action(
        "aggiungi pasta e pomodori alla lista della spesa",
        KITCHEN,
        "shopping-list-add",
        _DONE,
        item="pasta e pomodori",
    ),
    _action("add to the shopping list", KITCHEN, "shopping-list-add", _MORE),
    # ---- a reminder: what, and when if one is said
    _action(
        "remind me to call the dentist", KITCHEN, "reminder-add", _DONE, what="call the dentist"
    ),
    _action(
        "remind me to take the bins out tomorrow",
        DISPLAY,
        "reminder-add",
        _DONE,
        what="take the bins out",
        when="tomorrow",
    ),
    _action(
        "ricordami di chiamare mia madre",
        PHONE,
        "reminder-add",
        _DONE,
        what="chiamare mia madre",
    ),
    _action(
        "ricordami di pagare la bolletta venerdì",
        KITCHEN,
        "reminder-add",
        _DONE,
        what="pagare la bolletta",
        when="venerdì",
    ),
    _action("remind me", KITCHEN, "reminder-add", _MORE),
    # ---- the television: a channel, a number that is said in words or in digits
    _action("put on channel five", KITCHEN, "tv-channel", _DONE, channel=5),
    _action("metti il canale 7", DISPLAY, "tv-channel", _DONE, channel=7),
    _action("switch to channel twelve", PHONE, "tv-channel", _DONE, channel=12),
    _action("vai sul canale venti", KITCHEN, "tv-channel", _DONE, channel=20),
    _action("change the channel", KITCHEN, "tv-channel", _MORE),
    _action("metti il canale 1200", DISPLAY, "tv-channel", _MORE, channel=1200),
    # ---- music: on the device that is spoken to, and what to play if one is said
    _action("play some jazz", KITCHEN, "music-play", _DONE, query="jazz"),
    _action("play Bohemian Rhapsody", DISPLAY, "music-play", _DONE, query="Bohemian Rhapsody"),
    _action("play some music", PHONE, "music-play", _DONE),
    _action("metti Vasco Rossi", KITCHEN, "music-play", _DONE, query="Vasco Rossi"),
    _action("suona Bohemian Rhapsody", PHONE, "music-play", _DONE, query="Bohemian Rhapsody"),
    # ---- the front door: it cannot be undone, so a shared speaker may not and a yes is asked for
    _action("lock the front door", DISPLAY, "door-lock", _ASKS, action="lock"),
    _action("chiudi a chiave la porta di casa", PHONE, "door-lock", _ASKS, action="lock"),
    _action("unlock the front door", DISPLAY, "door-lock", _ASKS, action="unlock"),
    _action("sblocca la porta", DISPLAY, "door-lock", _ASKS, action="unlock"),
    _action("lock the front door", KITCHEN, "door-lock", _REFUSED, action="lock"),
    _action("lock or unlock the door", DISPLAY, "door-lock", _MORE),
    # ---- the coffee: a kind from a list, and how many if one is said
    _action("make an espresso", KITCHEN, "coffee-brew", _DONE, kind="espresso"),
    _action("fammi un caffè lungo", DISPLAY, "coffee-brew", _DONE, kind="lungo"),
    _action("make two americanos", KITCHEN, "coffee-brew", _DONE, kind="americano", cups=2),
    _action("prepara tre espressi", PHONE, "coffee-brew", _DONE, kind="espresso", cups=3),
    _action("make me a coffee", KITCHEN, "coffee-brew", _MORE),
    # ---- the vacuum: the whole home, or a room of a list in the other language
    _action("start the vacuum", KITCHEN, "vacuum-clean", _DONE),
    _action("vacuum the bathroom", DISPLAY, "vacuum-clean", _DONE, room="bathroom"),
    _action("pulisci la cucina con l'aspirapolvere", PHONE, "vacuum-clean", _DONE, room="kitchen"),
    _action("aspirapolvere in camera da letto", KITCHEN, "vacuum-clean", _DONE, room="bedroom"),
    _action("avvia l'aspirapolvere", DISPLAY, "vacuum-clean", _DONE),
    _action("pulisci il salotto", KITCHEN, "vacuum-clean", _DONE, room="living room"),
    # ---- a message to a person: two texts, and it cannot be taken back
    _action(
        "send a message to Anna saying I am late",
        DISPLAY,
        "send-message",
        _ASKS,
        to="Anna",
        text="I am late",
    ),
    _action(
        "manda un messaggio a Marco: arrivo tra dieci minuti",
        PHONE,
        "send-message",
        _ASKS,
        to="Marco",
        text="arrivo tra dieci minuti",
    ),
    _action(
        "text Paolo that dinner is ready",
        DISPLAY,
        "send-message",
        _ASKS,
        to="Paolo",
        text="dinner is ready",
    ),
    _action("send a message to Anna", DISPLAY, "send-message", _MORE, to="Anna"),
    _action(
        "send a message to Anna saying I am late",
        KITCHEN,
        "send-message",
        _REFUSED,
        to="Anna",
        text="I am late",
    ),
    # ---- a result: a read, of a team that is said
    _action(
        "what was the score of the Juventus game",
        KITCHEN,
        "sports-score",
        _DONE,
        team="Juventus",
    ),
    _action("com'è finita l'ultima del Milan", DISPLAY, "sports-score", _DONE, team="Milan"),
    _action("how did Inter do last night", PHONE, "sports-score", _DONE, team="Inter"),
    _action("risultato del Napoli", KITCHEN, "sports-score", _DONE, team="Napoli"),
    _action("what is the score", KITCHEN, "sports-score", _MORE),
    # ---- the oven: a number in a range and a mode if one is said, near the thermostat
    _action("preheat the oven to 180", KITCHEN, "oven-preheat", _DONE, temperature=180),
    _action("preriscalda il forno a 200 gradi", DISPLAY, "oven-preheat", _DONE, temperature=200),
    _action(
        "preheat the oven to 220 with the fan",
        KITCHEN,
        "oven-preheat",
        _DONE,
        temperature=220,
        mode="fan",
    ),
    _action(
        "forno a 180 gradi con il grill",
        PHONE,
        "oven-preheat",
        _DONE,
        temperature=180,
        mode="grill",
    ),
    _action("preheat the oven", KITCHEN, "oven-preheat", _MORE),
    _action("preheat the oven to 500", DISPLAY, "oven-preheat", _MORE, temperature=500),
    # ---- the night mode: a yes or a no, which is the model's reading of the words
    _action("turn on the night mode", KITCHEN, "night-mode", _DONE, enabled=True),
    _action("disattiva la modalità notte", DISPLAY, "night-mode", _DONE, enabled=False),
    _action("night mode off", PHONE, "night-mode", _DONE, enabled=False),
    _action("attiva la modalità notte", KITCHEN, "night-mode", _DONE, enabled=True),
    _action("set the night mode", KITCHEN, "night-mode", _MORE),
    # ---- the fridge: a number that may have a decimal, in a range, near the thermostat
    _action("set the fridge to 4 degrees", KITCHEN, "fridge-temperature", _DONE, temperature=4),
    _action("set the fridge to 3.5 degrees", DISPLAY, "fridge-temperature", _DONE, temperature=3.5),
    _action("frigorifero a 4,5 gradi", KITCHEN, "fridge-temperature", _DONE, temperature=4.5),
    _action("metti il frigo a sei gradi", PHONE, "fridge-temperature", _DONE, temperature=6),
    _action("set the fridge temperature", KITCHEN, "fridge-temperature", _MORE),
    _action("set the fridge to 12 degrees", DISPLAY, "fridge-temperature", _MORE, temperature=12),
    # ---- questions that are about what these skills do, and ask for none of it
    _question("what temperature should the oven be for pizza?"),
    _question("how many degrees is too cold for a baby room?"),
    _question("who won the world cup in 2006?"),
    _question("what is the best way to open a rusty lock?"),
    _question("how do I clean a coffee machine?"),
    _question("come si pulisce un aspirapolvere?"),
    _question("what does a thermostat do?"),
    _question("perché la ventola del computer fa rumore?"),
    _question("who sings Bohemian Rhapsody?"),
    _question("how long should an espresso take?"),
    _question("what should I put on a shopping list for a week?"),
    _question("come si scrive un messaggio di auguri?"),
    _question("what channel is the news on?"),
    _question("quanto costa un forno nuovo?"),
    _question("why do blinds get stuck?"),
    _question("è vero che il caffè fa male?"),
    _question("what is a good name for a robot vacuum?"),
    _question("how do I remind myself to drink water?"),
)
