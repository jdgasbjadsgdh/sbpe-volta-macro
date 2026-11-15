import logging
import time

import cffi

from _remote import ffi, lib
from manager import PluginBase
import util

# Volta multi-shot macro: fire 2 shots, open inventory, swap primary away and
# back (resets the volta cooldown), close inventory, fire 2 more, and so on.
#
# Keys are injected as SDL key events straight into the game's event queue,
# so nothing is sent to other windows and no external macro tool is needed.
#
# With direct_swap the inventory is never opened: the plugin sends the same
# 'move item' command the inventory screen sends when you equip the item in
# its first slot, so movement is not interrupted.
#
# With eight_shots and a second Volta in that slot, each reset is one move
# that equips the other Volta, which leaves enough input buffer for 8 shots.

SDL_KEYDOWN = 0x300
SDL_KEYUP = 0x301

# WorldClient::handleModifyInv(const ModifyInv&) and ModifyInv::ModifyInv() in
# the steam client. the bytes at these addresses are checked before use, on
# any other build direct_swap turns itself off
HANDLE_MODIFY_INV = 0x4db870
HANDLE_MODIFY_INV_SIG = bytes.fromhex('5589e5565389ce83ec108b5d088b4948')
MODIFY_INV_CTOR = 0x53f670
MODIFY_INV_CTOR_SIG = bytes.fromhex('55c701')  # followed by the vtable address

MODINV_MOVE = 2
# queue wait meaning "in the same frame as the previous event"
SAME_FRAME = -1

# input buffer spent by every shoot press and every inventory move. the
# buffer holds 1000 and refills by 1 every millisecond
INPUT_COST = 75
# eight_shots mode
EIGHT = 8
# items to look through for a slot, the bag can hold more than the 64
# equipped-item lookup in gun() expects
MAX_ITEMS = 512

# give up waiting for the real shot to leave its gun / for the fresh guns to
# show up after this long, in seconds
SEED_TIMEOUT = 0.12
GUNS_TIMEOUT = 0.04
SLOT_MAIN = 0x100
SLOT_INV = 0xf00

# cdecl -> thiscall with one argument: int thunk(this, function, arg)
THISCALL_THUNK = bytes.fromhex(
    '5589e5'    # push ebp; mov ebp, esp
    '83e4f0'    # and esp, -16
    '83ec0c'    # sub esp, 12
    '8b4d08'    # mov ecx, [ebp+8]
    '8b450c'    # mov eax, [ebp+12]
    'ff7510'    # push [ebp+16]
    'ffd0'      # call eax
    '89ec5dc3'  # mov esp, ebp; pop ebp; ret
)

# _remote only exposes a handful of SDL functions, open the rest in ABI mode
sdlffi = cffi.FFI()
sdlffi.cdef('''
typedef struct {
    uint32_t type;
    uint32_t timestamp;
    uint32_t windowID;
    uint8_t state;
    uint8_t repeat;
    uint8_t padding2;
    uint8_t padding3;
    int32_t scancode;
    int32_t sym;
    uint16_t mod;
    uint32_t unused;
    uint8_t rest[24]; // pad to sizeof(SDL_Event)
} KeyEvent;

int SDL_PushEvent(KeyEvent *event);
int32_t SDL_GetKeyFromScancode(int32_t scancode);
uint32_t SDL_GetWindowID(void *window);
uint32_t SDL_GetTicks(void);

void *__stdcall VirtualAlloc(void *addr, size_t size, uint32_t type, uint32_t protect);
void *__stdcall GetCurrentProcess(void);
int __stdcall ReadProcessMemory(void *proc, const void *addr, void *buf, size_t size, size_t *read);
typedef int (*thiscall1)(void *self, void *fn, void *arg);
''')


class Plugin(PluginBase):
    def onInit(self):
        self.sdl = sdlffi.dlopen('SDL2.dll')

        num = ffi.new('int *')
        self.keyStates = lib.SDL_GetKeyboardState(num)

        # key names are SDL scancode names, same as in [plugin_keybinds]
        self.config.option('key_trigger', 'G')
        self.config.option('key_inventory', 'Tab')
        self.config.option('key_swap', 'A')
        self.config.option('key_shoot', 'D')
        self.config.option('shots', 6, 'int')
        # shots fired between swaps, the volta allows 2
        self.config.option('shots_per_swap', 2, 'int')
        # timings in seconds
        self.config.option('shot_hold_1', 0.03, 'float')
        self.config.option('shot_gap', 0.03, 'float')
        self.config.option('shot_hold_2', 0.15, 'float')
        # the last burst has its own gap, and its second shot is held this
        # long, the shots travel while it is held
        self.config.option('last_gap', 0.03, 'float')
        self.config.option('last_hold', 0.4, 'float')
        # delay between inventory key events (0 = one full key press per frame)
        self.config.option('step_delay', 0.0, 'float')
        # keys that are pressed again after the inventory closes if you are
        # still holding them, so opening the inventory doesn't stop you
        self.config.option('rehold_keys', 'Left Right Up Down')
        # swap without opening the inventory
        self.config.option('direct_swap', True, 'bool')
        # inventory slot holding the other primary, 0 = first slot (the one
        # the inventory cursor starts on)
        self.config.option('swap_slot', 0, 'int')
        # delay between releasing the last shot of a pair and the swap
        self.config.option('pre_swap_delay', 0.05, 'float')
        # keep shoot held through the swap, so the shot before it flies
        self.config.option('hold_through_swap', True, 'bool')
        # how long the shot before a swap is held before swapping
        self.config.option('swap_hold', 0.09, 'float')
        # fire a decoy second shot in every pair so the first one survives
        # the swap, see sacrificeQueue. shots = shots that actually land
        self.config.option('sacrifice', True, 'bool')
        # minimum time from the real shot's press to the swap, the swap
        # also waits until that shot has left its gun
        self.config.option('cycle_time', 0.05, 'float')
        self.config.option('first_cycle_time', 0.05, 'float')
        # ignore the trigger while the input buffer is below this
        self.config.option('min_buffer', 920, 'int')
        # delay between the swap and the next shot
        self.config.option('swap_delay', 0.01, 'float')
        # fire 8 shots instead of `shots`. needs a second Volta (ideally an
        # identical one) in swap_slot, plus direct_swap and sacrifice. every
        # cooldown reset then equips the other Volta with ONE inventory move
        # instead of swapping away and back with two, which is what leaves
        # enough input buffer for 8. without a second Volta it fires `shots`
        self.config.option('eight_shots', False, 'bool')
        # input buffer kept spare during an 8-shot burst, so a movement key
        # pressed mid-burst can't make the game drop one of the macro's inputs
        self.config.option('buffer_reserve', 75, 'int')

        self.queue = []  # (wait since previous event, action, scancode)
        self.due = 0
        self.anchor = 0
        self.realId = 0
        self.wasDown = False
        self.direct = None  # (thunk, vtable) once set up, False if unusable
        self.single = False  # this burst resets with one move (eight_shots)
        self.seen = set()  # real shots of this burst seen in flight
        self.shootKey = 0
        self.fired = 0  # real shots fired this burst

    def readMem(self, addr, size):
        buf = sdlffi.new('uint8_t[]', size)
        ok = self.k32.ReadProcessMemory(
            self.k32.GetCurrentProcess(), sdlffi.cast('void *', addr),
            buf, size, sdlffi.NULL)
        return bytes(sdlffi.buffer(buf)) if ok else None

    def initDirect(self):
        self.direct = False
        self.k32 = sdlffi.dlopen('kernel32.dll')
        if self.readMem(HANDLE_MODIFY_INV, 16) != HANDLE_MODIFY_INV_SIG:
            logging.error('direct_swap: unknown client build, using keys')
            return
        ctor = self.readMem(MODIFY_INV_CTOR, 7)
        if ctor is None or ctor[:3] != MODIFY_INV_CTOR_SIG:
            logging.error('direct_swap: unknown client build, using keys')
            return
        vtable = int.from_bytes(ctor[3:], 'little')
        # MEM_COMMIT | MEM_RESERVE, PAGE_EXECUTE_READWRITE
        mem = self.k32.VirtualAlloc(sdlffi.NULL, 4096, 0x3000, 0x40)
        if mem == sdlffi.NULL:
            logging.error('direct_swap: VirtualAlloc failed, using keys')
            return
        sdlffi.memmove(mem, THISCALL_THUNK, len(THISCALL_THUNK))
        self.direct = (sdlffi.cast('thiscall1', mem), vtable)
        logging.info('direct_swap ready')

    def swap(self):
        wc = self.refs.WorldClient
        if not self.direct or wc == ffi.NULL:
            return
        thunk, vtable = self.direct
        # ModifyInv: classptr, command, slotpos, targetslotpos, _cached_size,
        # _has_bits
        slot = SLOT_INV + self.config.swap_slot
        msg = sdlffi.new('uint32_t[6]',
                         [vtable, MODINV_MOVE, slot, SLOT_MAIN, 0, 7])
        thunk(sdlffi.cast('void *', int(ffi.cast('uintptr_t', wc))),
              sdlffi.cast('void *', HANDLE_MODIFY_INV), msg)

    def scancode(self, name):
        k = lib.SDL_GetScancodeFromName(name.encode())
        if k == 0:
            logging.error('key name not recognized: "{}"'.format(name))
        return k

    def push(self, sc, down):
        ev = sdlffi.new('KeyEvent *')
        ev.type = SDL_KEYDOWN if down else SDL_KEYUP
        ev.timestamp = self.sdl.SDL_GetTicks()
        win = int(ffi.cast('uintptr_t', self.refs.window_[0]))
        ev.windowID = self.sdl.SDL_GetWindowID(sdlffi.cast('void *', win))
        ev.state = 1 if down else 0
        ev.scancode = sc
        ev.sym = self.sdl.SDL_GetKeyFromScancode(sc)
        self.sdl.SDL_PushEvent(ev)

    def start(self):
        cfg = self.config
        inv = self.scancode(cfg.key_inventory)
        swap = self.scancode(cfg.key_swap)
        shoot = self.scancode(cfg.key_shoot)
        if 0 in (inv, swap, shoot):
            return

        step = max(cfg.step_delay, 0)
        if cfg.direct_swap and self.direct is None:
            self.initDirect()
        direct = cfg.direct_swap and self.direct
        if direct and not 0 <= cfg.swap_slot < 0x100:
            logging.error('swap_slot out of range')
            return
        self.single = False
        self.seen = set()
        self.shootKey = shoot
        self.fired = 0
        if cfg.eight_shots and not (direct and cfg.sacrifice):
            logging.error('eight_shots needs direct_swap and sacrifice, '
                          'firing the normal burst')
        if direct and cfg.sacrifice:
            # the sequence uses nearly the whole input buffer, starting on a
            # part-filled one drops inputs half way through
            cw = self.refs.ClientWorld
            if cw != ffi.NULL and cw.inputBuffer < cfg.min_buffer:
                return
            shots = cfg.shots
            if cfg.eight_shots:
                if self.spareVolta():
                    shots = EIGHT
                    self.single = True
                else:
                    logging.error(
                        'eight_shots: inventory slot {} does not hold a second '
                        'Volta, firing {} shots'.format(cfg.swap_slot + 1, shots))
            self.queue = self.sacrificeQueue(shoot, shots)
            self.due = 0
            return
        q = []
        wait = 0

        def tap(sc, hold=step, after=step):
            nonlocal wait
            q.append((wait, 'down', sc))
            q.append((hold, 'up', sc))
            wait = after

        shots = max(cfg.shots, 1)
        per = max(cfg.shots_per_swap, 1)
        for n in range(shots):
            first = n % per == 0  # first shot after a swap
            if n > 0 and first and direct and cfg.hold_through_swap:
                pass  # already swapped, see below
            elif n > 0 and first and direct:
                # swap to the other primary and back to reset the cooldown
                # both swaps go out in one frame, the next shot in a later one
                q.append((wait, 'swap', 0))
                wait = max(cfg.swap_delay, 0)
            elif n > 0 and first:
                for sc in (inv, swap, swap, inv):
                    if step > 0:
                        tap(sc)
                    else:
                        # press and release in the same frame
                        q.append((wait, 'tap', sc))
                        wait = 0
                q.append((wait, 'rehold', 0))
                wait = 0
            if n == shots - 1:
                tap(shoot, cfg.last_hold)
            elif n % per == per - 1 and direct and cfg.hold_through_swap:
                # the newest shot blows up at once if shoot is not held when
                # it leaves the gun (about 75ms after the press), and a swap
                # before that cancels it. so keep shoot held until it is out,
                # then swap, release and press the next shot all in one frame
                q.append((wait, 'down', shoot))
                q.append((max(cfg.swap_hold, 0), 'swap', 0))
                q.append((SAME_FRAME, 'up', shoot))
                wait = SAME_FRAME
            elif n % per == per - 1:
                # next comes a swap. the shot only leaves the gun about 75ms
                # after the key press and a swap before that cancels it
                tap(shoot, cfg.shot_hold_1 if first else cfg.shot_hold_2,
                    max(cfg.pre_swap_delay, 0))
            else:
                lastburst = n // per == (shots - 1) // per
                tap(shoot, cfg.shot_hold_1,
                    cfg.last_gap if lastburst else cfg.shot_gap)

        self.queue = q
        self.due = 0

    def sacrificeQueue(self, shoot, shots):
        '''
        How the volta (HoverGun) works in the client:
        - it has two guns, each can fire once, a swap gives two fresh guns
        - a shot leaves its gun about 75ms after the press, a swap before
          that loses it
        - the gun keeps track of its newest shot only. unequipping destroys
          that shot, and it blows up on the spot if shoot is not held
        - equipping destroys every damage orb that already exists
        So of each pair only the first shot can survive a swap, and only
        once the second press has taken over as "newest". Every cycle here
        is: press (the real shot), press again (the decoy), wait for the
        real shot to leave its gun, swap. The last pair is not followed by
        a swap, so both of its shots are real.

        In eight_shots mode each swap is one inventory move to the other
        Volta, so a cycle costs 225 input buffer instead of 300. Before each
        real shot the plugin waits until the buffer holds the whole cycle,
        and if an earlier real shot has already landed it stops swapping
        (a swap would erase that shot's damage) and keeps the decoy as the
        last real shot instead. see beforeUpdate
        '''
        cfg = self.config
        q = []
        for i in range(max(shots - 2, 0)):
            # real shot. after a swap, downR waits for the fresh guns to
            # have been updated once, before that they do not aim up/down
            q.append((0, 'downR' if i > 0 else 'downA', shoot))
            q.append((0, 'mark', 0))                # remember the real shot
            q.append((SAME_FRAME, 'up', shoot))
            q.append((SAME_FRAME, 'down', shoot))   # decoy
            # swapA waits at least this long and then for the real shot to
            # have left its gun
            cycle = cfg.first_cycle_time if i == 0 else cfg.cycle_time
            q.append((max(cycle, 0), 'swapA', 0))
            q.append((SAME_FRAME, 'up', shoot))
        q.append((0, 'downR' if len(q) else 'down', shoot))
        q.append((0, 'up', shoot))
        q.append((SAME_FRAME, 'down', shoot))
        q.append((max(cfg.last_hold, 0), 'up', shoot))
        return q

    def item(self, slot):
        '''the item in a slot, None if empty.
        PlayerCharacter.items: unordered_map<int, Item*>,
        node = [next, key, Item*]'''
        cw = self.refs.ClientWorld
        if cw == ffi.NULL or cw.player == ffi.NULL:
            return None
        pc = ffi.cast('struct PlayerCharacter *', cw.player)
        ht = pc.items.t
        node = ffi.cast('void **', ht.before_begin)
        n = 0
        while node != ffi.NULL and n < min(ht.element_count, MAX_ITEMS):
            key = int(ffi.cast('uintptr_t', node[1])) & 0xffff
            if key == slot and node[2] != ffi.NULL:
                return node[2]
            node = ffi.cast('void **', node[0])
            n += 1
        return None

    def gun(self):
        '''the equipped primary if it is a HoverGun'''
        it = self.item(SLOT_MAIN)
        if it is None or util.getClassName(it) != 'HoverGun':
            return None
        return ffi.cast('struct HoverGun *', it)

    def spareVolta(self):
        '''is the equipped primary a Volta, with another one in swap_slot?'''
        if self.gun() is None:
            return False
        spare = self.item(SLOT_INV + self.config.swap_slot)
        return spare is not None and util.getClassName(spare) == 'HoverGun'

    def landed(self):
        '''has a real shot of this burst landed? it was seen flying and its
        seed is gone, so its damage orbs exist and a swap would erase them'''
        if not self.seen:
            return False
        objs = self.myObjects()
        return any(i not in objs for i in self.seen)

    def bufferReady(self):
        '''eight_shots: is there input buffer for the whole cycle that this
        real shot starts (real + decoy, plus the swap if one follows)?'''
        cw = self.refs.ClientWorld
        if cw == ffi.NULL:
            return True
        swaps = any(a == 'swapA' for _, a, _ in self.queue)
        need = INPUT_COST * (3 if swaps else 2) + max(self.config.buffer_reserve, 0)
        return cw.inputBuffer >= need

    def finishPair(self):
        '''eight_shots: a real shot landed before this pair's swap. skip the
        swap and the rest of the burst, the decoy that is already flying
        becomes the last real shot and is held like one'''
        self.queue = [(max(self.config.last_hold, 0), 'up', self.shootKey)]
        logging.info('eight_shots: a shot landed, finishing after {} shots '
                     'so its damage is kept'.format(self.fired + 1))

    def myObjects(self):
        cw = self.refs.ClientWorld
        if cw == ffi.NULL or cw.mySubWorld == ffi.NULL:
            return {}
        objs = util.worldobjects(cw.mySubWorld.asNativeSubWorld)
        return dict((o.objId, o) for o in objs)

    def seedOut(self, now):
        '''has the real shot of this cycle left its gun?'''
        if self.realId in self.myObjects():
            self.seen.add(self.realId)
            return True
        return now >= self.anchor + SEED_TIMEOUT

    def gunsReady(self, now):
        '''have the guns made by the swap been updated yet?'''
        g = self.gun()
        if g is None or now >= self.due + GUNS_TIMEOUT:
            return True
        objs = self.myObjects()
        for i in range(2):
            o = objs.get(g.gunObjIds[i])
            if o is None or (o.props.xmp == 0 and o.props.ymp == 0):
                return False
        return True

    def rehold(self):
        # keyStates is the physical keyboard, injected events don't touch it
        for name in self.config.rehold_keys.split():
            sc = lib.SDL_GetScancodeFromName(name.encode())
            if sc != 0 and self.keyStates[sc]:
                self.push(sc, True)

    def beforeUpdate(self):
        down = False
        if not lib.SDL_IsTextInputActive():
            trig = self.scancode(self.config.key_trigger)
            down = trig != 0 and self.keyStates[trig] != 0
        pressed = down and not self.wasDown
        self.wasDown = down

        if len(self.queue) == 0:
            if pressed and self.refs.WorldClient != ffi.NULL:
                self.start()
            else:
                return

        # events pushed here are picked up by the game's event loop right
        # after this callback. one key per frame, the game can miss keys that
        # arrive together
        now = time.perf_counter()
        sent = False
        while len(self.queue) > 0:
            wait, action = self.queue[0][0], self.queue[0][1]
            # swapA is timed from the last downA, everything else from the
            # previous event
            since = self.anchor if action == 'swapA' else self.due
            if wait != SAME_FRAME and (sent or now < since + wait):
                break
            if action == 'swapA' and not self.seedOut(now):
                break
            if action == 'swapA' and self.single and self.landed():
                self.finishPair()
                continue
            if action == 'downR' and not self.gunsReady(now):
                break
            if self.single and action in ('downA', 'downR') \
                    and not self.bufferReady():
                break
            if self.single and action == 'swapA' and \
                    self.refs.ClientWorld != ffi.NULL and \
                    self.refs.ClientWorld.inputBuffer < INPUT_COST:
                break
            _, action, sc = self.queue.pop(0)
            if action == 'mark':
                # the game has handled the real press by now, the gun's
                # newest shot is the real one until the decoy press
                g = self.gun()
                self.realId = g.out if g is not None else 0
                continue
            if action in ('downA', 'downR'):
                self.anchor = now
                self.fired += 1
                action = 'down'
            elif action == 'swapA':
                action = 'swap'
            if action == 'rehold':
                self.rehold()
                continue
            if action == 'swap':
                # eight_shots: one move equips the other Volta, otherwise
                # swap to the other primary and back
                self.swap()
                if not self.single:
                    self.swap()
            elif action == 'tap':
                self.push(sc, True)
                self.push(sc, False)
            else:
                self.push(sc, action == 'down')
            self.due = now
            sent = True

    def __del__(self):
        # don't leave keys stuck down if reloaded mid-sequence
        for _, action, sc in self.queue:
            if action == 'up':
                self.push(sc, False)
