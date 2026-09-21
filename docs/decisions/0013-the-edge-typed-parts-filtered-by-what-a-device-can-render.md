# 0013. The edge: typed parts, filtered by what a device can render

Status: accepted, 2026-09-21.

## Context

Devices are small and different: a speaker with a microphone and no screen, a display with both, a
screen with neither, a streamer that can be told to play something and cannot be spoken to, a
phone. The same answer has to be right for each of them: spoken for the first, spoken and shown
(with its sources) for the second, only shown on the third. Some of them will be a Raspberry Pi in
another room, behind a router, that restarts when the power goes, and something else in the system
(the `actions` service) has to be able to tell one of them to do something and to know whether it
did.

Three things are easy to get wrong. A device that has to wait for the end of an answer is silent for
as long as the model takes, which is what makes a voice assistant feel slow. A conversation service
that knows what a speaker is ends up with the shape of every device in it. And a protocol that
lets the server send what a device cannot render means every device carries the code to ignore it.

## Decision

**A device speaks a small versioned protocol over a WebSocket, in JSON text frames.** The device
speaks first (`hello`: the protocol it speaks, who it is, what it can do) and is told whether it is
known (`ready`). What a person said goes as text (`utterance`); the answer comes as a sequence of
`part` frames and a `turn_ended`; something for the device to do comes as a `command` and is
answered with an `ack`. The whole of version 1 is one module of contracts, and a frame that is not
one of them is refused without being echoed.

WebSocket because the traffic goes both ways (a command is pushed to a device that is asked for
nothing), a device behind a router connects out and no port is opened at home, a browser speaks it
too (the web app will show the same parts), and there is nothing more to run or secure. MQTT wants a
broker, which is a second piece to run and to secure for what one connection does; gRPC is heavy for
a microcontroller and for a browser; polling and server-sent events cannot carry what the device
says. Text frames, not binary, so that anyone can read what goes over the wire; audio is a part of
the protocol that does not exist yet (slice 8) and will be new frames, not a change to these.

**An answer is a sequence of typed parts, and the edge chooses which of them a device is sent.**
Each kind of part declares the capability that renders it (speech needs `audio_out`, text and cards
need `display`; progress is a hint for anybody). The conversation service streams text and says
which documents it cited, and knows nothing about devices; the edge cuts the text into sentences as
it arrives and makes of each what the device can render, and a part that a device cannot render is
never sent to it. The first sound is heard after the first sentence, and a speaker is never read
the list of its sources.

**A connection answers one utterance at a time, and speaking over an answer interrupts it.** A
person who says something while the assistant is talking wants the assistant to stop and listen.
The edge stops reading the conversation service's stream (which stops the model), ends the old
answer as `cancelled`, and answers the new one. There is no queue of answers.

**The last connection of a device wins.** When a device connects that already has a connection, the
old one is closed (4001). A dead connection can look alive for a long time, and a device that is
connecting is the one that is there.

**Commands go over the same connection and come back with a result, never with an error.** The
edge sends the command and waits for the `ack` with its id; a device that did not declare what the
command needs is `unsupported` without being sent anything, one that does not answer in time is
`timeout`, one that is not connected is `offline`. These are states a home is in every day, so they
are results of the command. Inside the platform a connected device is an implementation of the
`SystemDevice` interface, the one the fakes of the tests implement, so what sends commands does
not know whether the device is real.

**Who a device is comes from a registry the edge asks, and who it acts for from what is registered,
never from what the device says.** A device presents a credential; the registry says whether it is
known, what it is registered for (what it declares beyond that is ignored, and said, so that
whoever installs it finds out) and whom it acts for: the tenant for a shared device, which then has
no conversation of its own (ADR 0012), a user for a personal one. Until the hub exists the registry
is a YAML file and the edge speaks for a device to the conversation service by development
headers; both refuse to be built outside development and tests, and the environment defaults to
production. Both are interfaces (`DeviceRegistry`, `PrincipalDelegation`) that the hub replaces.

**The client of the first slice is a device in a terminal, with the profile of each kind of
device, and it checks the edge.** It renders only what its hardware could, keeps the state of that
hardware (a volume, a light, music, a screen) so that commands can be seen to work, and says
`[BUG]` when the edge sends what it must not: a part the device cannot render, parts out of order,
an answer nobody asked for. The profiles are the devices of the example file, and a test says so.

## What follows

- The edge is a place where a device is looked at from any side before there is one: the terminal,
  and later the Raspberry Pi client and the web app, speak the same protocol.
- The conversation service stays free of devices. A new kind of part (audio, a list to pick from)
  is a change to the contracts and to the edge, and nothing else.
- What is built on top is decided elsewhere and is not here: who may command a device is
  `authorize()` and the device's policy (slice 5a), how a device is paired and its credential
  issued is the hub (slice 7), and TLS and the network the devices are on are the deployment's.
- A command reaches a device only through the edge process that holds its connection. Running more
  than one edge needs a bus to route commands, as the design says, and nothing in the protocol
  needs to change for it.
- Sentence by sentence is a compromise: it gives the first sound early and cannot take back a
  sentence, so a model that starts to answer and changes its mind is heard doing it. It is the
  price of a voice that does not wait for the end.
