import os
import random

import cocotb
from cocotb.triggers import RisingEdge

from bfm.tx import TB, configure_basic, pipe_get
from model.tx import DecodedGdrpSubmission, TxReferenceModel, TxScoreboard, make_token


SEED = int(os.getenv("TX_MVP_SEED", "20260807"), 0)
REQUEST_COUNT = int(os.getenv("TX_MVP_REQUEST_COUNT", "128"), 0)


@cocotb.test(timeout_time=20_000_000, timeout_unit="ns")
async def randomized_requests_match_reference_model(dut):
    rng = random.Random(SEED)
    tb = TB(dut)
    await tb.reset()
    model = TxReferenceModel()
    await configure_basic(tb, model, max_len=4096)

    submissions = []
    expected = []
    for index in range(REQUEST_COUNT):
        submission = DecodedGdrpSubmission(
            qpn=0,
            token=make_token(
                slot_id=0,
                length=rng.randint(1, 4096),
                debug_seq=index & 0xF,
            ),
        )
        outputs = model.submit(submission)
        submissions.append(submission)
        expected.extend(outputs)

    async def producer():
        for submission in submissions:
            await tb.submission.enq(submission.pack())

    async def consumer():
        for index, expected_request in enumerate(expected):
            for _ in range(rng.randint(0, 5)):
                await RisingEdge(tb.clock)
            TxScoreboard.compare(
                expected_request,
                await pipe_get(tb.dma, tb.clock, cycles=10_000),
                event=f"seed={SEED} burst={index}",
            )

    producer_task = cocotb.start_soon(producer())
    consumer_task = cocotb.start_soon(consumer())
    await producer_task
    await consumer_task
    dut._log.info(
        "PASS seed=%d requests=%d bursts=%d", SEED, REQUEST_COUNT, len(expected)
    )
