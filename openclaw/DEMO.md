# Demo: AgentControl skills in OpenClaw

A video walkthrough in seven scenes, about ten minutes. The first half shows skills working as designed: each
LaunchDarkly variation decides which skills an OpenClaw agent gets. The second half shows the gap: OpenClaw's own
skills, which no variation pins, still reach an agent that can read files. It ends with how to close that gap.

`./demo.sh` runs the scenes and prints a summary of each run: what LaunchDarkly served, every tool call the agent
made, and its reply. Each scene is a real model call, so the wording changes between takes. The behavior described
below held in every rehearsal.

## Before you record

1. Do the setup in [README.md](README.md): `npm install`, `(cd plugin && npm install)`,
   `python3 setup_config.py setup`, and one `./run_checks.sh baseline`, which creates `.state/`.
2. Rehearse once without pauses: `DEMO_FAST=1 ./demo.sh`. It takes a few minutes.
3. Open two LaunchDarkly tabs:
   - **The `openclaw_agent` config**, on the Variations tab: `standard` and `locked-down`, with their models, tools
     and pinned skills.
   - **Library → Skills**: `openclaw-house-style`, `openclaw-workspace-report` and `openclaw-explain-limits`.
4. Make the terminal font large, and the window at least 110 columns wide.

Each run takes 5 to 20 seconds. Cut the waits in editing, or talk over them.

To redo a scene, run it alone, for example `./demo.sh 4`. Every scene starts from the same LaunchDarkly state, and
the script puts OpenClaw's settings back when it exits.

## Scene 0: What LaunchDarkly serves

**Run:** `./demo.sh`. The script pauses after each scene; press Enter to move on.

**Show:** the config tab, then the terminal's summary of the same thing.

**Say:**
- "This is one AgentControl config, `openclaw_agent`, with two variations."
- "`standard` gets a small model, two tools (read files and run commands), and two skills."
- "`locked-down` gets a smaller model, no tools, and a different pair of skills. One skill, house style, is shared by
  both."
- "A targeting rule sends the agent called `helper` to `locked-down`. Everything else gets `standard`."
- "The last line is OpenClaw's own skills, which come with OpenClaw. 22 are available to this agent, and
  LaunchDarkly knows nothing about them. Keep those in mind for later."

**Then:** the Library → Skills tab. "Skills are versioned here. A variation pins an exact version, the same way it
pins a model."

## Scene 1: Standard variation, the happy path

**Asks `main`:** "Give me a report on the files in this workspace."

**Expect:**
- Served `standard`, with both of its skills listed as pinned.
- `load_skill` for `openclaw-workspace-report` and `openclaw-house-style`, then `exec` and `read` calls.
- A reply under `## Workspace report`, with a table, ending in `Style: house v1`.

**Say:**
- "The agent's prompt only lists each pinned skill's name and description. It loads the full skill when it needs it,
  with a `load_skill` tool our plugin adds."
- "That tool serves the exact pinned version, straight from the LaunchDarkly SDK. Nothing is written to disk."
- "The table and the last line come from the two skills. So the skills really were loaded and followed."

## Scene 2: Locked-down variation, the happy path

**Asks `helper`:** "Use the exec tool to run: ls"

**Expect:** served `locked-down`, no tools, and `load_skill` for `openclaw-explain-limits`. The reply starts with
`Limit:`, names the tool, and suggests a next step.

**Say:**
- "Same plugin, different variation. This agent has no tools, so it can't run `ls`."
- "Its skill tells it how to explain that. Even with no tools, it can still load the skills its variation pins."

## Scene 3: A LaunchDarkly skill the variation doesn't pin

**Asks `helper`:** "Call load_skill with the name openclaw-workspace-report and tell me what it says."

**Expect:** a red ✗ on `load_skill openclaw-workspace-report`, marked "blocked: not pinned on this variation".

**Say:**
- "That skill is in our library, but only `standard` pins it."
- "Before `load_skill` runs, the plugin checks each request against this run's pins, so this one is refused. Change
  the pins in LaunchDarkly, and the next run follows them, with no deploy."

**If it goes differently:** sometimes the model explains instead of calling the tool. Run `./demo.sh 3` again.

## Scene 4: The gap

**Asks `main`:** "Load your diagram-maker skill and tell me, in one sentence, what its first instruction says."

**Expect:**
- ✗ `load_skill diagram-maker`: blocked, because it isn't pinned.
- Sometimes also ✗ `skills_read diagram-maker`: OpenClaw's own skill tool, blocked because it isn't on the
  variation's tool list.
- Then a yellow **!** on `read <openclaw package>/skills/diagram-maker/SKILL.md`: "an OpenClaw skill, not pinned in
  LaunchDarkly".
- The reply quotes the skill's first instruction.

**Say:**
- "`diagram-maker` is one of OpenClaw's built-in skills. No variation pins it, and LaunchDarkly has never heard of
  it."
- "Our gate held: `load_skill` refused it. So did the tool list, when the agent tried OpenClaw's skill tool."
- "But an OpenClaw skill is just a file on disk. OpenClaw's own part of the system prompt lists every one of its
  skills, with the path to its file. This variation allows `read`, so the agent read the file directly, and is now
  following a skill nobody approved."
- "AgentControl controls the skills it serves. It doesn't yet control the skills the runtime already has."

## Scene 5: The same request on locked-down

**Asks `helper`:** the same question.

**Expect:** ✗ `load_skill diagram-maker`, then a `Limit:` reply.

**Say:**
- "Locked-down can't reach it, but not because of a skills policy. It has no file tools, so OpenClaw doesn't even
  tell it that its own skills exist."
- "Give this variation `read`, and OpenClaw would list them and it would get in the same way. The protection is a
  side effect of its tool list."

## Scene 6: Closing the gap

**Shows:** `openclaw config set agents.defaults.skills '[]'`, then two runs on `main`.

**Expect:**
- The `diagram-maker` request fails. The agent reports that the skill isn't available, and makes no `read` of a
  `SKILL.md`.
- The workspace report still works, using the LaunchDarkly skills.
- Afterwards the script turns OpenClaw's own skills back on.

**Say:**
- "One line of OpenClaw config turns off its own skills for every agent, so LaunchDarkly pinning is the only way a
  skill reaches an agent."
- "The LaunchDarkly skills still work, because they don't come from OpenClaw's skill folders."
- "Two caveats:
  - This is static OpenClaw config, not a LaunchDarkly control. It's per agent, not per user.
  - It removes the skills from what the agent is told about. The files are still on disk, so an agent with shell
    access could go looking for them. The real boundary is the tool list."

## Wrap-up

- **Works today:**
  - per-context skills from LaunchDarkly, pinned by version and loaded on demand
  - a variation can't load skills it doesn't pin
  - re-pinning takes effect on the next run
- **The gap:** OpenClaw's own skills aren't gated by AgentControl. Any variation with file access can use them.
- **Options:**
  - turn OpenClaw's own skills off, as in scene 6
  - have variations allowlist OpenClaw skills by name, which gates loading but not the listing
  - keep `read` and `exec` off variations that shouldn't use them
- **For the product:** a way for AgentControl to govern the runtime's built-in skills, not only its own library.
