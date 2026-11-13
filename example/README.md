# Example project: "Two Humans"

A 30-second cut made by hand with cutvelvet, small enough to keep in the repository: twelve short
clips from a 1927 silent film, half a minute of Satie, one timeline, one published version with a
few review notes waiting on it, and four rules switched on. Sources and licences are in
[CREDITS.md](CREDITS.md).

```
cutvelvet serve example        # from the repository root, then open http://127.0.0.1:8771
```

The first time, give it a few seconds: thumbnails and preview proxies for the twelve clips are
built into `example/.cutvelvet/` (ignored by git).

Things to try:

- **Review**: play `demo_v1`. Shot S6 has a note, "crop is on his forehead — want the eyes", and a
  pinned clip. The crop really is on his forehead; it was left that way so there is something to fix.
- **Live**: go to S6, click the top strip, drag the crop box down onto his eyes, Save. Then
  **Publish**: one shot re-renders and `demo_v2` appears in Review.
- **Library**: every clip shows which seconds the cut already uses; `lantern-water` is not used at all.
- Or leave the crop alone and press **Run notes** to have your own agent act on the notes (this one
  is billed by your agent's provider).

From the command line:

```
cutvelvet audit  example                 # passes: no moment of footage is used twice
cutvelvet stills example demo 15,24      # two frames into example/.cutvelvet/stills/demo/
cutvelvet publish example demo --scale 0.5
```

Notes on what is committed:

- `review/demo_v1.mp4` was published at half scale and then re-encoded (H.264 CRF 27, about 2 MB)
  to keep the repository small; a fresh `publish` writes a much larger file.
- Shot boundaries in `timelines/demo.json` sit on the 24 fps frame grid (multiples of 0.125 s
  nearest each bar line of the music), so changing one shot re-renders only that shot.
- To put the example back as it was: `git checkout example && git clean -fd example`.
