"""

Copyright (c) 2020 Alex Forencich

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in
all copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN
THE SOFTWARE.

"""

import cocotb
from cocotb.triggers import RisingEdge, Event
from cocotb.log import SimLog

from collections import deque

from .version import __version__
from .constants import *
from .axi_channels import *


class AxiMasterWrite(object):
    def __init__(self, entity, name, clock, reset=None):
        self.log = SimLog("cocotb.%s.%s" % (entity._name, name))

        self.log.info("AXI master model")
        self.log.info("cocotbext-axi version %s", __version__)
        self.log.info("Copyright (c) 2020 Alex Forencich")
        self.log.info("https://github.com/alexforencich/cocotbext-axi")

        self.reset = reset

        self.aw_channel = AxiAWSource(entity, name, clock, reset)
        self.w_channel = AxiWSource(entity, name, clock, reset)
        self.b_channel = AxiBSink(entity, name, clock, reset)

        self.active_tokens = set()

        self.write_command_queue = deque()
        self.write_command_sync = Event()
        self.write_resp_queue = deque()
        self.write_resp_sync = Event()
        self.write_resp_set = set()

        self.id_queue = deque(range(2**len(self.aw_channel.bus.awid)))
        self.id_sync = Event()

        self.int_write_resp_command_queue = deque()
        self.int_write_resp_command_sync = Event()
        self.int_write_resp_queue_list = {}

        self.in_flight_operations = 0

        self.width = len(self.w_channel.bus.wdata)
        self.byte_size = 8
        self.byte_width = self.width // self.byte_size
        self.strb_mask = 2**self.byte_width-1

        self.max_burst_len = 256
        self.max_burst_size = (self.byte_width-1).bit_length()

        assert self.byte_width == len(self.w_channel.bus.wstrb)
        assert self.byte_width * self.byte_size == self.width

        assert len(self.b_channel.bus.bid) == len(self.aw_channel.bus.awid)

        cocotb.fork(self._process_write())
        cocotb.fork(self._process_write_resp())

    def init_write(self, address, data, burst=AxiBurstType.INCR, size=None, lock=AxiLockType.NORMAL, cache=0b0011, prot=AxiProt.NONSECURE, qos=0, region=0, user=0, token=None):
        if token is not None:
            if token in self.active_tokens:
                raise Exception("Token is not unique")
            self.active_tokens.add(token)

        self.in_flight_operations += 1

        self.write_command_queue.append((address, data, burst, size, lock, cache, prot, qos, region, user, token))
        self.write_command_sync.set()

    def idle(self):
        return not self.in_flight_operations

    async def wait(self):
        while not self.idle():
            self.write_resp_sync.clear()
            await self.write_resp_sync.wait()

    async def wait_for_token(self, token):
        if token not in self.active_tokens:
            return
        while token not in self.write_resp_set:
            self.write_resp_sync.clear()
            await self.write_resp_sync.wait()

    def write_resp_ready(self, token=None):
        if token is not None:
            return token in self.write_resp_set
        return bool(self.write_resp_queue)

    def get_write_resp(self, token=None):
        if token is not None:
            if token in self.write_resp_set:
                for resp in self.write_resp_queue:
                    if resp[-1] == token:
                        self.write_resp_queue.remove(resp)
                        self.active_tokens.remove(resp[-1])
                        self.write_resp_set.remove(resp[-1])
                        return resp
            return None
        if self.write_resp_queue:
            resp = self.write_resp_queue.popleft()
            if resp[-1] is not None:
                self.active_tokens.remove(resp[-1])
                self.write_resp_set.remove(resp[-1])
            return resp
        return None

    async def write(self, address, data, burst=AxiBurstType.INCR, size=None, lock=AxiLockType.NORMAL, cache=0b0011, prot=AxiProt.NONSECURE, qos=0, region=0, user=0):
        token = object()
        self.init_write(address, data, burst, size, lock, cache, prot, qos, region, user, token)
        await self.wait_for_token(token)
        return self.get_write_resp(token)[1:3]

    async def write_words(self, address, data, ws=2, burst=AxiBurstType.INCR, size=None, lock=AxiLockType.NORMAL, cache=0b0011, prot=AxiProt.NONSECURE, qos=0, region=0, user=0):
        words = data
        data = bytearray()
        for w in words:
            data.extend(w.to_bytes(ws, 'little'))
        await self.write(address, data, burst, size, lock, cache, prot, qos, region, user)

    async def write_dwords(self, address, data, burst=AxiBurstType.INCR, size=None, lock=AxiLockType.NORMAL, cache=0b0011, prot=AxiProt.NONSECURE, qos=0, region=0, user=0):
        await self.write_words(address, data, 4, burst, size, lock, cache, prot, qos, region, user)

    async def write_qwords(self, address, data, burst=AxiBurstType.INCR, size=None, lock=AxiLockType.NORMAL, cache=0b0011, prot=AxiProt.NONSECURE, qos=0, region=0, user=0):
        await self.write_words(address, data, 8, burst, size, lock, cache, prot, qos, region, user)

    async def write_byte(self, address, data, burst=AxiBurstType.INCR, size=None, lock=AxiLockType.NORMAL, cache=0b0011, prot=AxiProt.NONSECURE, qos=0, region=0, user=0):
        await self.write(address, [data], burst, size, lock, cache, prot, qos, region, user)

    async def write_word(self, address, data, ws=2, burst=AxiBurstType.INCR, size=None, lock=AxiLockType.NORMAL, cache=0b0011, prot=AxiProt.NONSECURE, qos=0, region=0, user=0):
        await self.write_words(address, [data], ws, burst, size, lock, cache, prot, qos, region, user)

    async def write_dword(self, address, data, burst=AxiBurstType.INCR, size=None, lock=AxiLockType.NORMAL, cache=0b0011, prot=AxiProt.NONSECURE, qos=0, region=0, user=0):
        await self.write_dwords(address, [data], burst, size, lock, cache, prot, qos, region, user)

    async def write_qword(self, address, data, burst=AxiBurstType.INCR, size=None, lock=AxiLockType.NORMAL, cache=0b0011, prot=AxiProt.NONSECURE, qos=0, region=0, user=0):
        await self.write_qwords(address, [data], burst, size, lock, cache, prot, qos, region, user)

    async def _process_write(self):
        while True:
            if not self.write_command_queue:
                self.write_command_sync.clear()
                await self.write_command_sync.wait()

            address, data, burst, size, lock, cache, prot, qos, region, user, token = self.write_command_queue.popleft()

            num_bytes = self.byte_width

            if size is None:
                size = self.max_burst_size
            else:
                num_bytes = 2**size
                assert 0 < num_bytes <= self.byte_width

            aligned_addr = (address // num_bytes) * num_bytes
            word_addr = (address // self.byte_width) * self.byte_width

            start_offset = address % self.byte_width
            end_offset = ((address + len(data) - 1) % self.byte_width) + 1

            cycles = (len(data) + (address % num_bytes) + num_bytes-1) // num_bytes

            cur_addr = aligned_addr
            offset = 0
            cycle_offset = aligned_addr-word_addr
            n = 0
            transfer_count = 0

            burst_list = []
            burst_length = 0

            self.log.info(f"Write start addr: {address:#010x} prot: {prot} data: {' '.join((f'{c:02x}' for c in data))}")

            for k in range(cycles):
                start = cycle_offset
                stop = cycle_offset+num_bytes

                if k == 0:
                    start = start_offset
                if k == cycles-1:
                    stop = end_offset

                strb = (self.strb_mask << start) & self.strb_mask & (self.strb_mask >> (self.byte_width - stop))

                val = 0
                for j in range(start, stop):
                    val |= bytearray(data)[offset] << j*8
                    offset += 1

                if n >= burst_length:
                    if not self.id_queue:
                        self.id_sync.clear()
                        await self.id_sync.wait()

                    awid = self.id_queue.popleft()

                    transfer_count += 1
                    n = 0

                    burst_length = min(cycles-k, min(max(self.max_burst_len, 1), 256)) # max len
                    burst_length = (min(burst_length*num_bytes, 0x1000-(cur_addr&0xfff))+num_bytes-1)//num_bytes # 4k align

                    burst_list.append((awid, burst_length))

                    aw = self.aw_channel._transaction_obj()
                    aw.awid = awid
                    aw.awaddr = cur_addr
                    aw.awlen = burst_length-1
                    aw.awsize = size
                    aw.awburst = burst
                    aw.awlock = lock
                    aw.awcache = cache
                    aw.awprot = prot
                    aw.awqos = qos
                    aw.awregion = region
                    aw.awuser = user

                    await self.aw_channel.drive(aw)

                    self.log.info(f"Write burst start awid {awid:#x} awaddr: {cur_addr:#010x} awlen: {burst_length-1} awsize: {size}")

                n += 1

                w = self.w_channel._transaction_obj()
                w.wdata = val
                w.wstrb = strb
                w.wlast = n >= burst_length

                self.w_channel.send(w)

                cur_addr += num_bytes
                cycle_offset = (cycle_offset + num_bytes) % self.byte_width

            self.int_write_resp_command_queue.append((address, len(data), size, cycles, prot, burst_list, token))
            self.int_write_resp_command_sync.set()

    async def _process_write_resp(self):
        while True:
            if not self.int_write_resp_command_queue:
                self.int_write_resp_command_sync.clear()
                await self.int_write_resp_command_sync.wait()

            addr, length, size, cycles, prot, burst_list, token = self.int_write_resp_command_queue.popleft()

            resp = AxiResp.OKAY
            user = []

            for bid, burst_length in burst_list:
                self.int_write_resp_queue_list.setdefault(bid, deque())
                while True:
                    if self.int_write_resp_queue_list[bid]:
                        break

                    await self.b_channel.wait()
                    b = self.b_channel.recv()

                    self.int_write_resp_queue_list[int(b.bid)].append(b)

                b = self.int_write_resp_queue_list[bid].popleft()

                burst_id = int(b.bid)
                burst_resp = AxiResp(b.bresp)
                burst_user = int(b.buser)

                if burst_resp != AxiResp.OKAY:
                    resp = burst_resp

                if burst_user is not None:
                    user.append(burst_user)

                if bid in self.id_queue:
                    raise Exception(f"Unexpected burst ID {bid}")
                self.id_queue.append(bid)
                self.id_sync.set()

                self.log.info(f"Write burst complete bid {burst_id:#x} bresp: {burst_resp!s}")

            self.log.info(f"Write complete addr: {addr:#010x} prot: {prot} resp: {resp!s} length: {length}")

            self.write_resp_queue.append((addr, length, resp, user, token))
            self.write_resp_sync.set()
            if token is not None:
                self.write_resp_set.add(token)
            self.in_flight_operations -= 1


class AxiMasterRead(object):
    def __init__(self, entity, name, clock, reset=None):
        self.log = SimLog("cocotb.%s.%s" % (entity._name, name))

        self.reset = reset

        self.ar_channel = AxiARSource(entity, name, clock, reset)
        self.r_channel = AxiRSink(entity, name, clock, reset)

        self.active_tokens = set()

        self.read_command_queue = deque()
        self.read_command_sync = Event()
        self.read_data_queue = deque()
        self.read_data_sync = Event()
        self.read_data_set = set()

        self.id_queue = deque(range(2**len(self.ar_channel.bus.arid)))
        self.id_sync = Event()

        self.int_read_resp_command_queue = deque()
        self.int_read_resp_command_sync = Event()
        self.int_read_resp_queue_list = {}

        self.in_flight_operations = 0

        self.width = len(self.r_channel.bus.rdata)
        self.byte_size = 8
        self.byte_width = self.width // self.byte_size

        self.max_burst_len = 256
        self.max_burst_size = (self.byte_width-1).bit_length()

        assert self.byte_width * self.byte_size == self.width

        assert len(self.r_channel.bus.rid) == len(self.ar_channel.bus.arid)

        cocotb.fork(self._process_read())
        cocotb.fork(self._process_read_resp())

    def init_read(self, address, length, burst=AxiBurstType.INCR, size=None, lock=AxiLockType.NORMAL, cache=0b0011, prot=AxiProt.NONSECURE, qos=0, region=0, user=0, token=None):
        if token is not None:
            if token in self.active_tokens:
                raise Exception("Token is not unique")
            self.active_tokens.add(token)

        self.in_flight_operations += 1

        self.read_command_queue.append((address, length, burst, size, lock, cache, prot, qos, region, user, token))
        self.read_command_sync.set()

    def idle(self):
        return not self.in_flight_operations

    async def wait(self):
        while not self.idle():
            self.read_resp_sync.clear()
            await self.read_resp_sync.wait()

    async def wait_for_token(self, token):
        if token not in self.active_tokens:
            return
        while token not in self.read_data_set:
            self.read_data_sync.clear()
            await self.read_data_sync.wait()

    def read_data_ready(self, token=None):
        if token is not None:
            return token in self.read_data_set
        return bool(self.read_data_queue)

    def get_read_data(self, token=None):
        if token is not None:
            if token in self.read_data_set:
                for resp in self.read_data_queue:
                    if resp[-1] == token:
                        self.read_data_queue.remove(resp)
                        self.active_tokens.remove(resp[-1])
                        self.read_data_set.remove(resp[-1])
                        return resp
            return None
        if self.read_data_queue:
            resp = self.read_data_queue.popleft()
            if resp[-1] is not None:
                self.active_tokens.remove(resp[-1])
                self.read_data_set.remove(resp[-1])
            return resp
        return None

    async def read(self, address, length, burst=AxiBurstType.INCR, size=None, lock=AxiLockType.NORMAL, cache=0b0011, prot=AxiProt.NONSECURE, qos=0, region=0, user=0):
        token = object()
        self.init_read(address, length, burst, size, lock, cache, prot, qos, region, user, token)
        await self.wait_for_token(token)
        return self.get_read_data(token)[1:3]

    async def read_words(self, address, count, ws=2, burst=AxiBurstType.INCR, size=None, lock=AxiLockType.NORMAL, cache=0b0011, prot=AxiProt.NONSECURE, qos=0, region=0, user=0):
        data = await self.read(address, count*ws, burst, size, lock, cache, prot, qos, region, user)
        words = []
        for k in range(count):
            words.append(int.from_bytes(data[0][ws*k:ws*(k+1)], 'little'))
        return words

    async def read_dwords(self, address, count, burst=AxiBurstType.INCR, size=None, lock=AxiLockType.NORMAL, cache=0b0011, prot=AxiProt.NONSECURE, qos=0, region=0, user=0):
        return await self.read_words(address, count, 4, burst, size, lock, cache, prot, qos, region, user)

    async def read_qwords(self, address, count, burst=AxiBurstType.INCR, size=None, lock=AxiLockType.NORMAL, cache=0b0011, prot=AxiProt.NONSECURE, qos=0, region=0, user=0):
        return await self.read_words(address, count, 8, burst, size, lock, cache, prot, qos, region, user)

    async def read_byte(self, address, burst=AxiBurstType.INCR, size=None, lock=AxiLockType.NORMAL, cache=0b0011, prot=AxiProt.NONSECURE, qos=0, region=0, user=0):
        return (await self.read(address, 1, burst, size, lock, cache, prot, qos, region, user))[0]

    async def read_word(self, address, ws=2, burst=AxiBurstType.INCR, size=None, lock=AxiLockType.NORMAL, cache=0b0011, prot=AxiProt.NONSECURE, qos=0, region=0, user=0):
        return (await self.read_words(address, 1, ws, burst, size, lock, cache, prot, qos, region, user))[0]

    async def read_dword(self, address, burst=AxiBurstType.INCR, size=None, lock=AxiLockType.NORMAL, cache=0b0011, prot=AxiProt.NONSECURE, qos=0, region=0, user=0):
        return (await self.read_dwords(address, 1, burst, size, lock, cache, prot, qos, region, user))[0]

    async def read_qword(self, address, burst=AxiBurstType.INCR, size=None, lock=AxiLockType.NORMAL, cache=0b0011, prot=AxiProt.NONSECURE, qos=0, region=0, user=0):
        return (await self.read_qwords(address, 1, burst, size, lock, cache, prot, qos, region, user))[0]

    async def _process_read(self):
        while True:
            if not self.read_command_queue:
                self.read_command_sync.clear()
                await self.read_command_sync.wait()

            address, length, burst, size, lock, cache, prot, qos, region, user, token = self.read_command_queue.popleft()

            num_bytes = self.byte_width

            if size is None:
                size = self.max_burst_size
            else:
                num_bytes = 2**size
                assert 0 < num_bytes <= self.byte_width

            aligned_addr = (address // num_bytes) * num_bytes
            word_addr = (address // self.byte_width) * self.byte_width

            cycles = (length + num_bytes-1 + (address % num_bytes)) // num_bytes

            burst_list = []

            cur_addr = aligned_addr
            n = 0

            burst_length = 0

            for k in range(cycles):

                n += 1
                if n >= burst_length:
                    if not self.id_queue:
                        self.id_sync.clear()
                        await self.id_sync.wait()

                    arid = self.id_queue.popleft()

                    n = 0

                    burst_length = min(cycles-k, min(max(self.max_burst_len, 1), 256)) # max len
                    burst_length = (min(burst_length*num_bytes, 0x1000-(cur_addr&0xfff))+num_bytes-1)//num_bytes # 4k align

                    burst_list.append((arid, burst_length))

                    ar = self.r_channel._transaction_obj()
                    ar.arid = arid
                    ar.araddr = cur_addr
                    ar.arlen = burst_length-1
                    ar.arsize = size
                    ar.arburst = burst
                    ar.arlock = lock
                    ar.arcache = cache
                    ar.arprot = prot
                    ar.arqos = qos
                    ar.arregion = region
                    ar.aruser = user

                    await self.ar_channel.drive(ar)

                    self.log.info(f"Read burst start arid {arid:#x} araddr: {cur_addr:#010x} arlen: {burst_length-1} arsize: {size}")

                cur_addr += num_bytes

            self.int_read_resp_command_queue.append((address, length, size, cycles, prot, burst_list, token))
            self.int_read_resp_command_sync.set()

    async def _process_read_resp(self):
        while True:
            if not self.int_read_resp_command_queue:
                self.int_read_resp_command_sync.clear()
                await self.int_read_resp_command_sync.wait()

            addr, length, size, cycles, prot, burst_list, token = self.int_read_resp_command_queue.popleft()

            num_bytes = 2**size

            aligned_addr = (addr // num_bytes) * num_bytes
            word_addr = (addr // self.byte_width) * self.byte_width

            start_offset = addr % self.byte_width
            end_offset = ((addr + length - 1) % self.byte_width) + 1

            cycle_offset = aligned_addr - word_addr
            data = bytearray()

            resp = AxiResp.OKAY
            user = []

            first = True

            for rid, burst_length in burst_list:
                for k in range(burst_length):
                    self.int_read_resp_queue_list.setdefault(rid, deque())
                    while True:
                        if self.int_read_resp_queue_list[rid]:
                            break

                        await self.r_channel.wait()
                        r = self.r_channel.recv()

                        self.int_read_resp_queue_list[int(r.rid)].append(r)

                    r = self.int_read_resp_queue_list[rid].popleft()

                    cycle_id = int(r.rid)
                    cycle_data = int(r.rdata)
                    cycle_resp = AxiResp(r.rresp)
                    cycle_last = int(r.rlast)
                    cycle_user = int(r.ruser)

                    if cycle_resp != AxiResp.OKAY:
                        resp = cycle_resp

                    if cycle_user is not None:
                        user.append(cycle_user)

                    start = cycle_offset
                    stop = cycle_offset+num_bytes

                    if first:
                        start = start_offset

                    assert cycle_last == (k == burst_length - 1)

                    for j in range(start, stop):
                        data.append((cycle_data >> j*8) & 0xff)

                    cycle_offset = (cycle_offset + num_bytes) % self.byte_width

                    first = False

                if rid in self.id_queue:
                    raise Exception(f"Unexpected burst ID {rid}")
                self.id_queue.append(rid)
                self.id_sync.set()

                self.log.info(f"Read burst complete rid {cycle_id:#x} rresp: {resp!s}")

            data = data[:length]

            self.log.info(f"Read complete addr: {addr:#010x} prot: {prot} resp: {resp!s} data: {' '.join((f'{c:02x}' for c in data))}")

            self.read_data_queue.append((addr, data, resp, user, token))
            self.read_data_sync.set()
            if token is not None:
                self.read_data_set.add(token)
            self.in_flight_operations -= 1


class AxiMaster(object):
    def __init__(self, entity, name, clock, reset=None):
        self.write_if = None
        self.read_if = None

        self.write_if = AxiMasterWrite(entity, name, clock, reset)
        self.read_if = AxiMasterRead(entity, name, clock, reset)

    def init_read(self, address, length, burst=AxiBurstType.INCR, size=None, lock=AxiLockType.NORMAL, cache=0b0011, prot=AxiProt.NONSECURE, qos=0, region=0, user=0, token=None):
        self.read_if.init_read(address, length, burst, size, lock, cache, prot, qos, region, user, token)

    def init_write(self, address, data, burst=AxiBurstType.INCR, size=None, lock=AxiLockType.NORMAL, cache=0b0011, prot=AxiProt.NONSECURE, qos=0, region=0, user=0, token=None):
        self.write_if.init_write(address, data, burst, size, lock, cache, prot, qos, region, user, token)

    def idle(self):
        return (not self.read_if or self.read_if.idle()) and (not self.write_if or self.write_if.idle())

    async def wait(self):
        while not self.idle():
            await self.write_if.wait()
            await self.read_if.wait()

    async def wait_read(self):
        await self.read_if.wait()

    async def wait_write(self):
        await self.write_if.wait()

    def read_data_ready(self, token=None):
        return self.read_if.read_data_ready(token)

    def get_read_data(self, token=None):
        return self.read_if.get_read_data(token)

    def write_resp_ready(self, token=None):
        return self.write_if.write_resp_ready(token)

    def get_write_resp(self, token=None):
        return self.write_if.get_write_resp(token)

    async def read(self, address, length, burst=AxiBurstType.INCR, size=None, lock=AxiLockType.NORMAL, cache=0b0011, prot=AxiProt.NONSECURE, qos=0, region=0, user=0):
        return await self.read_if.read(address, length, burst, size, lock, cache, prot, qos, region, user)

    async def read_words(self, address, count, ws=2, burst=AxiBurstType.INCR, size=None, lock=AxiLockType.NORMAL, cache=0b0011, prot=AxiProt.NONSECURE, qos=0, region=0, user=0):
        return await self.read_if.read_words(address, count, ws, burst, size, lock, cache, prot, qos, region, user)

    async def read_dwords(self, address, count, burst=AxiBurstType.INCR, size=None, lock=AxiLockType.NORMAL, cache=0b0011, prot=AxiProt.NONSECURE, qos=0, region=0, user=0):
        return await self.read_if.read_dwords(address, count, burst, size, lock, cache, prot, qos, region, user)

    async def read_qwords(self, address, count, burst=AxiBurstType.INCR, size=None, lock=AxiLockType.NORMAL, cache=0b0011, prot=AxiProt.NONSECURE, qos=0, region=0, user=0):
        return await self.read_if.read_qwords(address, count, burst, size, lock, cache, prot, qos, region, user)

    async def read_byte(self, address, burst=AxiBurstType.INCR, size=None, lock=AxiLockType.NORMAL, cache=0b0011, prot=AxiProt.NONSECURE, qos=0, region=0, user=0):
        return await self.read_if.read_byte(address, burst, size, lock, cache, prot, qos, region, user)

    async def read_word(self, address, ws=2, burst=AxiBurstType.INCR, size=None, lock=AxiLockType.NORMAL, cache=0b0011, prot=AxiProt.NONSECURE, qos=0, region=0, user=0):
        return await self.read_if.read_word(address, ws, burst, size, lock, cache, prot, qos, region, user)

    async def read_dword(self, address, burst=AxiBurstType.INCR, size=None, lock=AxiLockType.NORMAL, cache=0b0011, prot=AxiProt.NONSECURE, qos=0, region=0, user=0):
        return await self.read_if.read_dword(address, burst, size, lock, cache, prot, qos, region, user)

    async def read_qword(self, address, burst=AxiBurstType.INCR, size=None, lock=AxiLockType.NORMAL, cache=0b0011, prot=AxiProt.NONSECURE, qos=0, region=0, user=0):
        return await self.read_if.read_qword(address, burst, size, lock, cache, prot, qos, region, user)

    async def write(self, address, data, burst=AxiBurstType.INCR, size=None, lock=AxiLockType.NORMAL, cache=0b0011, prot=AxiProt.NONSECURE, qos=0, region=0, user=0):
        return await self.write_if.write(address, data, burst, size, lock, cache, prot, qos, region, user)

    async def write_words(self, address, data, ws=2, burst=AxiBurstType.INCR, size=None, lock=AxiLockType.NORMAL, cache=0b0011, prot=AxiProt.NONSECURE, qos=0, region=0, user=0):
        return await self.write_if.write_words(address, data, ws, burst, size, lock, cache, prot, qos, region, user)

    async def write_dwords(self, address, data, burst=AxiBurstType.INCR, size=None, lock=AxiLockType.NORMAL, cache=0b0011, prot=AxiProt.NONSECURE, qos=0, region=0, user=0):
        return await self.write_if.write_dwords(address, data, burst, size, lock, cache, prot, qos, region, user)

    async def write_qwords(self, address, data, burst=AxiBurstType.INCR, size=None, lock=AxiLockType.NORMAL, cache=0b0011, prot=AxiProt.NONSECURE, qos=0, region=0, user=0):
        return await self.write_if.write_qwords(address, data, burst, size, lock, cache, prot, qos, region, user)

    async def write_byte(self, address, data, burst=AxiBurstType.INCR, size=None, lock=AxiLockType.NORMAL, cache=0b0011, prot=AxiProt.NONSECURE, qos=0, region=0, user=0):
        return await self.write_if.write_byte(address, data, burst, size, lock, cache, prot, qos, region, user)

    async def write_word(self, address, data, ws=2, burst=AxiBurstType.INCR, size=None, lock=AxiLockType.NORMAL, cache=0b0011, prot=AxiProt.NONSECURE, qos=0, region=0, user=0):
        return await self.write_if.write_word(address, data, ws, burst, size, lock, cache, prot, qos, region, user)

    async def write_dword(self, address, data, burst=AxiBurstType.INCR, size=None, lock=AxiLockType.NORMAL, cache=0b0011, prot=AxiProt.NONSECURE, qos=0, region=0, user=0):
        return await self.write_if.write_dword(address, data, burst, size, lock, cache, prot, qos, region, user)

    async def write_qword(self, address, data, burst=AxiBurstType.INCR, size=None, lock=AxiLockType.NORMAL, cache=0b0011, prot=AxiProt.NONSECURE, qos=0, region=0, user=0):
        return await self.write_if.write_qword(address, data, burst, size, lock, cache, prot, qos, region, user)


