import cocotb
from cocotb.triggers import RisingEdge

from bfm.tx import TB, configure_basic, pipe_get
from model.tx import DecodedGdrpSubmission, TxReferenceModel, TxScoreboard, make_token


@cocotb.test()
async def blocked_dma_output_keeps_an_elastic_ordered_pipeline(dut):
    tb = TB(dut)
    await tb.reset()
    model = TxReferenceModel()
    await configure_basic(tb, model, max_len=4096)

    submissions = [
        DecodedGdrpSubmission(
            qpn=0,
            token=make_token(slot_id=0, length=64 + index, debug_seq=index),
        )
        for index in range(6)
    ]
    expected = [model.submit(submission)[0] for submission in submissions]

    await tb.submission.enq(submissions[0].pack())
    while not await tb.dma.not_empty():
        await RisingEdge(tb.clock)

    held = await tb.dma.first()
    producers = [
        cocotb.start_soon(tb.submission.enq(submission.pack()))
        for submission in submissions[1:]
    ]

    for _ in range(80):
        assert await tb.dma.first() == held
        await RisingEdge(tb.clock)

    # More than the two-entry Frontend FIFO has accepted work.  Therefore
    # younger WQEs have advanced into later elastic stages while DMA is held.
    assert sum(task.done() for task in producers) >= 4
    TxScoreboard.compare(expected[0], held, event="blocked[0]")
    await tb.dma.deq()

    for index, expected_request in enumerate(expected[1:], start=1):
        TxScoreboard.compare(
            expected_request,
            await pipe_get(tb.dma, tb.clock, cycles=10_000),
            event=f"blocked[{index}]",
        )

    for task in producers:
        await task
