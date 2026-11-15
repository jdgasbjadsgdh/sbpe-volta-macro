# SBPE Volta multi-shot macro (`volta6.py`)

An SBPE plugin for **StarBreak** (Windows Steam client)
that turns one key press into a long burst from the **Volta**.

Normally the Volta fires 2 shots and then has to wait for its cooldown. Swapping
the primary weapon away and back gives it fresh guns. This plugin does
that for you: press **G** and it fires, swaps, fires again, and so on, timed so
that the shots survive each swap.

## What it does

Press the trigger key (default **G**). The plugin then:

1. Fires the Volta by injecting **shoot** key presses into the game.
2. Swaps your primary to the item in an inventory slot and back again, which
   resets the Volta's cooldown.
3. Repeats until it has fired `shots` shots. The last shot is held for
   `last_hold` seconds, so it keeps flying.

Key presses are pushed straight into the game's own SDL event queue
(`SDL_PushEvent`). Nothing goes to other windows, and you don't need AutoHotkey
or any other macro tool.

### Two ways to swap

| Mode | How the swap happens | Notes |
|---|---|---|
| `direct_swap = yes` (default) | Calls the client's own `WorldClient::handleModifyInv` with a "move item" request, the same request the inventory screen sends when you equip an item. | The inventory never opens, so your movement isn't interrupted. Works only on the client build it was made for (see below). |
| `direct_swap = no` | Presses Tab → swap key → swap key → Tab, like a person would, then presses any movement keys you were still holding. | Works on any build, but it opens the inventory and is slower. |

### Why the timing is so specific

The plugin's comments describe how the Volta (`HoverGun` in the client)
behaves:

- It has two guns. Each can fire once, and a swap gives you two fresh guns.
- A shot leaves its gun about **75 ms** after the press. If you swap before
  that, the shot is lost.
- The gun only tracks its **newest** shot. Unequipping the gun destroys that
  shot, and the shot blows up on the spot if shoot isn't held.
- Equipping the gun destroys every damage orb that already exists.

So with `sacrifice = yes` (the default), every pair works like this:

1. A **real** shot.
2. A **decoy** press, so the real shot is no longer the newest and survives the swap.
3. Wait until the real shot has actually left its gun. The plugin checks the
   game's object list for it and gives up after 120 ms.
4. Swap.
5. Wait until the fresh guns have updated (aimed). It gives up after 40 ms.
6. The next real shot.

The final pair isn't followed by a swap, so both of its shots are real. The
sequence uses almost the whole input buffer, so the trigger is ignored when the
buffer is below `min_buffer`. Starting on a part-filled buffer would drop
inputs halfway through.

## Install

1. Put `volta6.py` in the `plugins` folder of your SBPE install. That's the
   folder that also holds `pypy`, `manager.py` and `config.ini`.
2. Add a section to `config.ini`. The section name must match the file name:

   ```ini
   [plugin_volta6]
   enabled = yes
   key_trigger = G
   key_shoot = D
   ```

3. Start StarBreak through SBPE. In game, press **G** with the Volta equipped
   and your second primary in the first inventory slot.

Any option you leave out uses the default shown below. SBPE reloads plugins
while the game is running.

## Options

Key names are SDL scancode names, the same names used in `[plugin_keybinds]`.
Times are in seconds.

| Option | Default | What it does |
|---|---|---|
| `key_trigger` | `G` | Starts the macro (on press, not while held). |
| `key_shoot` | `D` | Your in-game shoot key. |
| `key_inventory` | `Tab` | Inventory key (key-press swap mode only). |
| `key_swap` | `A` | Key that equips the selected inventory item (key-press swap mode only). |
| `shots` | `6` | Total shots per trigger. With `sacrifice`, this counts shots that land. |
| `shots_per_swap` | `2` | Shots between swaps. The Volta allows 2. |
| `direct_swap` | `yes` | Swap through the client's inventory call instead of key presses. |
| `swap_slot` | `0` | Inventory slot holding your other primary. 0 is the first slot. |
| `sacrifice` | `yes` | Fire a decoy in each pair so the real shot survives the swap. |
| `cycle_time` / `first_cycle_time` | `0.05` | Minimum time from a real shot's press to the swap. |
| `min_buffer` | `920` | Ignore the trigger while the input buffer is below this. |
| `last_hold` | `0.4` | How long the final shot is held. |
| `hold_through_swap` | `yes` | Keep shoot held through the swap (no-sacrifice mode). |
| `swap_hold` | `0.09` | How long the shot before a swap is held (no-sacrifice mode). |
| `swap_delay` | `0.01` | Delay from a swap to the next shot (no-sacrifice mode). |
| `shot_hold_1`, `shot_hold_2`, `shot_gap`, `last_gap`, `pre_swap_delay` | `0.03`, `0.15`, `0.03`, `0.03`, `0.05` | Press lengths and gaps in key-press mode. |
| `step_delay` | `0.0` | Delay between inventory key events. 0 means one full press per frame. |
| `rehold_keys` | `Left Right Up Down` | Keys pressed again after the inventory closes, if you are still holding them. |

## Compatibility

- **Windows only.** It loads `SDL2.dll` and `kernel32.dll`.
- **Direct swap is tied to one client build.** It uses fixed addresses in the
  Steam client: `handleModifyInv` at `0x4db870` and the `ModifyInv`
  constructor at `0x53f670`. Before using them it checks the bytes at those
  addresses. On any other build it logs `direct_swap: unknown client build,
  using keys` and falls back to key presses.
- Direct swap writes a 31-byte calling thunk into memory that it allocates in the game process
  (`VirtualAlloc`, read/write/execute). That thunk lets Python call the
  client's `thiscall` function.
- The trigger is ignored while you type in chat (`SDL_IsTextInputActive`).
- If the plugin is reloaded in the middle of a burst, it releases any key it
  was still holding.

## Fair use

This is an input macro, and it calls the game client's own functions. Use it only where
macros and client plugins are allowed. On servers that ban them, using it can
get your account banned.
