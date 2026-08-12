# Day 8 — The Answer Reaches the Screen

Personal learning log. Not read by Claude automatically — this is for me, to
recall what I built and why once the project is done. Not needed as context for
future sessions or development; it's a record, not a spec.

Day 7 made the server able to answer. The only way to see it was `curl -N`.
Day 8 is the other half: a page that opens the stream, turns raw bytes back into
events, and paints words as they arrive. No new backend code — the endpoint
didn't change once all day. Everything here happens in the browser.

---

## The idea in one paragraph

The server sends Server-Sent Events: little labelled blocks of text separated by
blank lines, pushed down a connection that stays open. The browser's job is to
read bytes as they trickle in, work out where one event ends and the next begins,
and update the screen. That sounds like reading a file. It isn't, and almost
every hard part of the day came from the ways it isn't.

---

## Part 0 — The shape of the day

| Step | What | Why it came here |
|---|---|---|
| 1 | Types: `ChatEvent`, `Source`, `MODELS` | Every later file refers to them |
| 2 | `streamChat` in `lib/api.ts` | The stream reader — the actual engineering |
| 3 | `dashboard/chat/page.tsx` | The screen, which is mostly bookkeeping |
| 4 | Verify locally | Cheap, and catches the obvious |
| 5 | Verify live | The only verification that counts |

Step 5 is not a formality, and Day 8 is the day I learned that properly. See
Part 5.

---

## Part 1 — Why you can't just read the stream

The naive mental model is that the server sends an event and the browser
receives an event. That is not what happens. What the browser receives is
**bytes, cut wherever the network felt like cutting them.**

One read might hand me half an event. Or three events and a bit. The network has
no idea what an event is — it's moving packets, and packet boundaries have
nothing to do with my data's boundaries.

SSE's answer is a delimiter: every event ends with a blank line, `\n\n`. So the
reader keeps a `buffer` string, adds every new chunk to it, and then repeatedly
asks "is there a `\n\n` in here yet?" Everything before one is a complete event
and can be handed onward. Whatever is left over stays in the buffer and waits for
more bytes to complete it.

```ts
buffer += decoder.decode(value, { stream: true });

let split: number;
while ((split = buffer.indexOf("\n\n")) !== -1) {
  const block = buffer.slice(0, split);
  buffer = buffer.slice(split + 2);
  ...
}
```

Two loops, nested, and the nesting is the point: the outer one is paced by the
network, the inner one is paced by the data. One network read can produce zero
complete events or five.

### The `{ stream: true }` that looks optional

`decoder.decode(value)` would work almost all the time, which is the worst kind
of bug. A character like é or an em dash is more than one byte, and the network
can split it across two chunks. Decode each chunk independently and you get a
`�` where half a character landed.

`{ stream: true }` tells the decoder to hold onto a trailing partial character
and finish it with the next chunk. It's a one-word fix for a bug that would have
shown up rarely, looked like a model problem, and taken hours to trace.

---

## Part 2 — Why `streamChat` is an async generator

A normal `async` function returns once. This needs to hand back many things over
time, and the page needs to react to each one immediately.

An **async generator** (`async function*`) does exactly that: it `yield`s a value
and pauses until the caller asks for the next. The page consumes it with
`for await (const event of streamChat(...))` and reads like an ordinary loop,
even though each turn of it is separated by a network round trip.

The pay-off is that all the ugliness — buffering, splitting, decoding — lives
inside the generator, and the page only ever sees clean typed events. The page's
entire stream handler is a `switch` on four cases: `conversation`, `sources`,
`token`, `error`.

---

## Part 3 — The order of events is a design decision

The server sends `conversation` first, then `sources`, then tokens. That order
isn't arbitrary.

- `conversation` first, because a chat that started without an id needs to learn
  its id immediately — the next question has to attach to the same conversation.
- `sources` **before any token**, because the answer contains `[1]` and `[3]`
  markers. If sources arrived at the end, those markers would be meaningless
  numbers while the answer was being written, and only become citations after it
  finished. Sending them first means `[1]` is a real reference the moment it is
  typed.

A detail I'd have got wrong if I'd designed the client first and the protocol
second.

---

## Part 4 — Three ways a stream can end

This is the part of the day worth remembering.

I thought there were two. There are three, and I shipped code that handled two of
them.

| How it ends | What `reader.read()` does | What the user sees without a fix |
|---|---|---|
| Server hangs up cleanly | resolves `{ done: true }` | A half-answer that looks complete |
| Connection refused, DNS fails | rejects with `TypeError` | An error, handled |
| **The network vanishes** | **never settles** | **Frozen page** |

Row 1 I'd already thought about. `done: true` means "no more bytes", and the
reader cannot tell a finished response from a severed one — both just stop. So
the server always signs off with a `done` or `error` event, and the client
checks it arrived:

```ts
if (!signedOff && !signal.aborted) {
  throw new Error("The connection dropped before the answer finished.");
}
```

The reasoning I wrote in the comment still holds: *a half-answer that looks
finished is worse than a visible error, because you would trust it.*

Row 3 is the one that got me. When wifi disappears, **nothing closes the
socket.** A clean close requires the other end to send a packet saying so — and
the other end is exactly what just became unreachable. So the connection isn't
closed and isn't broken. It's silent. `reader.read()` returns a promise that is
never resolved and never rejected. It just sits there, possibly for minutes,
until the operating system gives up on the TCP connection.

And a generator parked on `await` is a generator that runs no more code. The
`throw` two lines below never happened. The page's `catch` never ran. Its
`finally` — the thing that re-enables the send button — never ran. The result was
a half-answer, a spinning "Thinking…", and a dead button.

### The fix: watch for silence

There is no signal to listen for, so the only evidence available is the absence
of evidence. A healthy stream is never quiet for long, because the server is
pushing tokens continuously. So: race every read against a timer that resets
whenever bytes arrive.

```ts
return await Promise.race([
  reader.read(),
  new Promise<never>((_, reject) => {
    idle = setTimeout(() => reject(new Error("The connection stalled...")), IDLE_MS);
  }),
]);
```

Start a read and a 20-second alarm together, take whichever finishes first.
Bytes arriving cancel the alarm; the next read sets a fresh one. So the countdown
restarts on every chunk and only a genuine 20-second gap can win. 20 seconds is
deliberately generous — a slow model must never trip it, and the cost of being
generous is only that a truly dead connection takes 20 seconds to report.

`clearTimeout` in a `finally` matters more than it looks. Without it, every read
would leave a live timer behind that fires later and rejects a promise nobody is
waiting on.

---

## Part 5 — The testing lesson, which is the real lesson

I tested the dropped-connection guard locally by making the server `return` early
from its generator, cutting the response short. The error appeared. I ticked the
box.

That test was **valid and irrelevant**. It exercised row 1 of the table — a
clean hang-up — which the guard already handled. The box I was ticking said
*"kill the network mid-stream"*, which is row 3, and the two are not the same
failure at all.

Worse, I'd already been told this by two failed attempts and hadn't listened.
DevTools "Offline" didn't kill the stream. Killing the server by hand didn't
either. Both failures were pointing at the same fact: **`localhost` traffic never
touches the network.** It's loopback — it never leaves the machine — so nothing I
did to the network could possibly affect it. There was no way to produce row 3
locally, and the substitute test I invented quietly changed the question to one I
could answer.

On the live site the stream really does travel over wifi to Railway. Switching
wifi off severed a real connection, and the page hung exactly as described above.

The takeaway isn't "test in production". It's narrower and more useful: **when a
test is hard to run, notice whether the easy substitute is testing the same
thing.** Mine wasn't, and it produced a green tick over a broken feature — which
is worse than no test, because it stopped me looking.

---

## Part 6 — Decisions that were deliberately boring

- **Native `<select>` for the model picker**, not a component library's dropdown.
  It's one element, it's keyboard accessible for free, and it works on mobile
  because the phone shows its own picker. A styled dropdown would have been more
  code to do the same job worse.
- **Native `<details>` for the Sources panel.** Expand/collapse with no state,
  no JavaScript, no library.
- **No new npm packages the whole day.**
- **Errors as an inline `role="alert"` line**, not toasts. There's no toast
  component in the repo and one error at a time is all this page can produce.
- **Route is `/dashboard/chat`, not `/dashboard/chat/[id]`.** BUILD.md said the
  latter. An id in the URL is only useful if you can load a conversation from it,
  and the endpoint for that doesn't exist until Day 9. Building the id route now
  would have meant a URL that looks meaningful and isn't. Day 9 moves the file
  into `[id]/`, which is a rename.

---

## What Day 8 cost

Two commits and two merges, because the second one was a bug the first one
shipped. That's the honest record and I'd rather it stayed in the history than
be squashed into a tidy single commit.

## Still open

- Cross-user RLS on retrieval is still untested — inherited from Day 7, not a
  Day 8 regression.
- Conversations don't persist in the UI. Reload the page and the chat is gone,
  even though every exchange is already saved in the database. That's Day 9.
