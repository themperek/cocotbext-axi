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

import itertools
import logging
import os
import random

import cocotb_test.simulator
import pytest

import cocotb
from cocotb.log import SimLog
from cocotb.clock import Clock
from cocotb.triggers import RisingEdge, Timer
from cocotb.regression import TestFactory

from cocotbext.axi import AxiMaster, AxiRam

class TB(object):
    def __init__(self, dut):
        self.dut = dut

        self.log = SimLog(f"cocotb.tb")
        self.log.setLevel(logging.DEBUG)

        cocotb.fork(Clock(dut.clk, 2, units="ns").start())

        self.axi_master = AxiMaster(dut, "axi", dut.clk, dut.rst)
        self.axi_ram = AxiRam(dut, "axi", dut.clk, dut.rst, size=2**16)

        self.axi_ram.write_if.log.setLevel(logging.DEBUG)
        self.axi_ram.read_if.log.setLevel(logging.DEBUG)

    def set_idle_generator(self, generator=None):
        if generator:
            self.axi_master.write_if.aw_channel.set_pause_generator(generator())
            self.axi_master.write_if.w_channel.set_pause_generator(generator())
            self.axi_master.read_if.ar_channel.set_pause_generator(generator())
            self.axi_ram.write_if.b_channel.set_pause_generator(generator())
            self.axi_ram.read_if.r_channel.set_pause_generator(generator())

    def set_backpressure_generator(self, generator=None):
        if generator:
            self.axi_master.write_if.b_channel.set_pause_generator(generator())
            self.axi_master.read_if.r_channel.set_pause_generator(generator())
            self.axi_ram.write_if.aw_channel.set_pause_generator(generator())
            self.axi_ram.write_if.w_channel.set_pause_generator(generator())
            self.axi_ram.read_if.ar_channel.set_pause_generator(generator())

    async def cycle_reset(self):
        self.dut.rst.setimmediatevalue(0)
        await RisingEdge(self.dut.clk)
        await RisingEdge(self.dut.clk)
        self.dut.rst <= 1
        await RisingEdge(self.dut.clk)
        await RisingEdge(self.dut.clk)
        self.dut.rst <= 0
        await RisingEdge(self.dut.clk)
        await RisingEdge(self.dut.clk)

async def run_test_write(dut, idle_inserter=None, backpressure_inserter=None, size=None):

    tb = TB(dut)

    byte_width = tb.axi_master.write_if.byte_width
    max_burst_size = tb.axi_master.write_if.max_burst_size

    if size is None:
        size = max_burst_size

    await tb.cycle_reset()

    tb.set_idle_generator(idle_inserter)
    tb.set_backpressure_generator(backpressure_inserter)

    for length in list(range(1,byte_width*2))+[1024]:
        for offset in list(range(byte_width))+list(range(4096-byte_width,4096)):
            tb.log.info(f"length {length}, offset {offset}")
            addr = offset+0x1000
            test_data = bytearray([x%256 for x in range(length)])

            tb.axi_ram.write_mem(addr-128, b'\xaa'*(length+256))

            await tb.axi_master.write(addr, test_data, size=size)

            tb.log.debug(tb.axi_ram.hexdump_str((addr&0xfffffff0)-16, (((addr&0xf)+length-1)&0xfffffff0)+48))

            assert tb.axi_ram.read_mem(addr, length) == test_data
            assert tb.axi_ram.read_mem(addr-1, 1) == b'\xaa'
            assert tb.axi_ram.read_mem(addr+length, 1) == b'\xaa'

    await RisingEdge(dut.clk)
    await RisingEdge(dut.clk)

async def run_test_read(dut, idle_inserter=None, backpressure_inserter=None, size=None):

    tb = TB(dut)

    byte_width = tb.axi_master.write_if.byte_width
    max_burst_size = tb.axi_master.write_if.max_burst_size

    if size is None:
        size = max_burst_size

    await tb.cycle_reset()

    tb.set_idle_generator(idle_inserter)
    tb.set_backpressure_generator(backpressure_inserter)

    for length in list(range(1,byte_width*2))+[1024]:
        for offset in list(range(byte_width))+list(range(4096-byte_width,4096)):
            tb.log.info(f"length {length}, offset {offset}")
            addr = offset+0x1000
            test_data = bytearray([x%256 for x in range(length)])

            tb.axi_ram.write_mem(addr, test_data)

            data = await tb.axi_master.read(addr, length, size=size)

            assert data[0] == test_data

    await RisingEdge(dut.clk)
    await RisingEdge(dut.clk)

async def run_stress_test(dut, idle_inserter=None, backpressure_inserter=None):

    tb = TB(dut)

    await tb.cycle_reset()

    tb.set_idle_generator(idle_inserter)
    tb.set_backpressure_generator(backpressure_inserter)

    async def stress_test_worker(master, offset, aperture, count=16):
        for k in range(count):
            length = random.randint(1, min(512, aperture))
            addr = offset+random.randint(0, aperture-length)
            test_data = bytearray([x%256 for x in range(length)])

            await Timer(random.randint(1, 100), 'ns')

            await master.write(addr, test_data)

            await Timer(random.randint(1, 100), 'ns')

            data = await master.read(addr, length)
            assert data[0] == test_data

    workers = []

    for k in range(16):
        workers.append(cocotb.fork(stress_test_worker(tb.axi_master, k*0x1000, 0x1000, count=16)))

    while workers:
        await workers.pop(0).join()

    await RisingEdge(dut.clk)
    await RisingEdge(dut.clk)

def cycle_pause():
    return itertools.cycle([1, 1, 1, 0])

if cocotb.SIM_NAME:

    data_width = int(os.getenv("PARAM_DATA_WIDTH"))
    byte_width = data_width // 8
    max_burst_size = (byte_width-1).bit_length()

    for test in [run_test_write, run_test_read]:

        factory = TestFactory(test)
        factory.add_option("idle_inserter", [None, cycle_pause])
        factory.add_option("backpressure_inserter", [None, cycle_pause])
        factory.add_option("size", [None]+list(range(max_burst_size)))
        factory.generate_tests()

    factory = TestFactory(run_stress_test)
    factory.generate_tests()


tests_dir = os.path.dirname(__file__)
rtl_dir = os.path.abspath(os.path.join(tests_dir, '..', '..', 'rtl'))

@pytest.mark.parametrize("data_width", [8, 16, 32])
def test_axi(request, data_width):
    dut = "test_axi"
    module = os.path.splitext(os.path.basename(__file__))[0]
    toplevel = dut

    verilog_sources = [
        os.path.join(tests_dir, f"{dut}.v"),
    ]

    parameters = {}

    parameters['DATA_WIDTH'] = data_width
    parameters['ADDR_WIDTH'] = 32
    parameters['STRB_WIDTH'] = parameters['DATA_WIDTH'] // 8
    parameters['ID_WIDTH'] = 8
    parameters['AWUSER_WIDTH'] = 1
    parameters['WUSER_WIDTH'] = 1
    parameters['BUSER_WIDTH'] = 1
    parameters['ARUSER_WIDTH'] = 1
    parameters['RUSER_WIDTH'] = 1

    extra_env = {f'PARAM_{k}' : str(v) for k, v in parameters.items()}

    sim_build = os.path.join(tests_dir,
        "sim_build_"+request.node.name.replace('[', '-').replace(']', ''))

    cocotb_test.simulator.run(
        python_search=[tests_dir],
        verilog_sources=verilog_sources,
        toplevel=toplevel,
        module=module,
        parameters=parameters,
        sim_build=sim_build,
        extra_env=extra_env,
    )

