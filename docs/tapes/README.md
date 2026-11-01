# Terminal screens

The screens in the main README come from the [VHS](https://github.com/charmbracelet/vhs) tapes in this folder. Each
tape types commands into a real terminal and records what ws prints, so the screens show the build in this checkout.

| Tape | Records |
|---|---|
| `setup.tape` | `docs/assets/ws-setup.gif` and `ws-setup-review.png`: the `ws setup` walk |
| `doctor.tape` | `docs/assets/ws-doctor.png`: `ws doctor` after setup |
| `gate.tape` | `docs/assets/ws-gate.png`: `ws gate replay` and one refusal |
| `_brand.tape` | The settings every tape shares: font, frame and the ws colours |

## Record them again

```sh
scripts/record_screens.sh          # every tape
scripts/record_screens.sh gate     # one tape
```

The script needs Docker and uv. It builds the wheel from this checkout, then runs VHS v0.10.0 from its container image,
pinned by digest, so every recording uses the same VHS, fonts and terminal. Nothing is installed on your machine.

## A made-up machine

The tapes run under `/home/demo`, a throwaway HOME that `demo-home.sh` builds inside the container. The repos, the
notes vault and the key file are invented, and the key file is not a key. The demo has no real AI tool: a stub answers
`claude --version` so setup has a runner to find. Doctor's ptrace line comes from the kernel of the machine that
records, so it can differ from one recording to the next.

Open every image before you commit it. The PII gate reads text, not pictures.
