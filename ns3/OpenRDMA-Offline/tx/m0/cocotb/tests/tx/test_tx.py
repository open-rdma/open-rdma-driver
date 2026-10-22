import cocotb

from bfm.tx import TB, configure, configure_basic, pipe_get
from model.tx import (
    DecodedGdrpSubmission,
    QpCfgReq,
    QpEntry,
    TxDmaReadRequest,
    TxReferenceModel,
    TxScoreboard,
    make_token,
)

# Header-aware packet count at pmtu 256 (configure_basic default): the RoCEv2
# first packet carries BTH+RETH (28 B overhead, 228 B payload limit) and
# continuations BTH (12 B, 244 B limit).  Must stay identical to the BSV
# txPacketCount / phasea packetCountForMessage and model/tx.py.
_FIRST_LIMIT = 228
_CONTINUATION_LIMIT = 244


def _psn_count_pmtu256(length: int) -> int:
    if length <= _FIRST_LIMIT:
        return 1
    return 1 + (
        length - _FIRST_LIMIT + _CONTINUATION_LIMIT - 1
    ) // _CONTINUATION_LIMIT


async def submit_and_compare(tb, model, submission, *, event, cycles=20_000):
    expected = model.submit(submission)
    await tb.submission.enq(submission.pack())
    for index, expected_burst in enumerate(expected):
        TxScoreboard.compare(
            expected_burst,
            await pipe_get(tb.dma, tb.clock, cycles=cycles),
            event=f"{event}[{index}]",
        )
    return expected


@cocotb.test()
async def valid_cross_page_request_matches_reference_model(dut):
    tb = TB(dut)
    await tb.reset()
    model = TxReferenceModel()
    await configure_basic(
        tb,
        model,
        local_offset=4096 - 16,
        max_len=32,
        mr_length=8192,
    )

    submission = DecodedGdrpSubmission(
        qpn=0, token=make_token(slot_id=0, length=32, debug_seq=9)
    )
    expected = await submit_and_compare(tb, model, submission, event="page-cross")
    assert len(expected) == 2

    first = TxDmaReadRequest.unpack(expected[0].pack())
    second = TxDmaReadRequest.unpack(expected[1].pack())
    assert int(first.dmaSeq) == 0 and int(first.isFirst) == 1
    assert int(first.isLast) == 0 and int(first.len) == 16
    assert int(second.dmaSeq) == 1 and int(second.isLast) == 1
    assert int(second.len) == 16


@cocotb.test()
async def one_byte_1024_and_1025_byte_boundaries(dut):
    tb = TB(dut)
    await tb.reset()
    model = TxReferenceModel()
    await configure_basic(tb, model, max_len=4096)

    cases = [(1, 1), (1024, 1), (1025, 2)]
    for debug_seq, (length, burst_count) in enumerate(cases):
        submission = DecodedGdrpSubmission(
            qpn=0,
            token=make_token(
                slot_id=0, length=length, debug_seq=debug_seq
            ),
        )
        expected = await submit_and_compare(
            tb, model, submission, event=f"length={length}"
        )
        assert len(expected) == burst_count
        assert sum(int(item.len) for item in expected) == length
        assert int(expected[0].psnCount) == _psn_count_pmtu256(length)


@cocotb.test()
async def physical_4k_boundary_is_split_after_huge_page_translation(dut):
    tb = TB(dut)
    await tb.reset()
    model = TxReferenceModel()
    await configure_basic(
        tb,
        model,
        local_offset=4096 - 256,
        max_len=512,
        mr_length=2 * 1024 * 1024,
        page_shift=21,
    )

    submission = DecodedGdrpSubmission(
        qpn=0, token=make_token(slot_id=0, length=512, debug_seq=4)
    )
    expected = await submit_and_compare(tb, model, submission, event="physical-4k")
    assert [int(item.len) for item in expected] == [256, 256]
    assert int(expected[0].physicalAddr) & 0xFFF == 0xF00
    assert int(expected[1].physicalAddr) & 0xFFF == 0


@cocotb.test()
async def sixty_four_kib_message_is_burst_major_and_complete(dut):
    tb = TB(dut)
    await tb.reset()
    model = TxReferenceModel()
    length = 64 * 1024
    await configure_basic(tb, model, max_len=length, mr_length=length)

    submission = DecodedGdrpSubmission(
        qpn=0, token=make_token(slot_id=0, length=length, debug_seq=6)
    )
    expected = await submit_and_compare(tb, model, submission, event="64KiB")
    assert len(expected) == 64
    assert [int(item.dmaSeq) for item in expected] == list(range(64))
    assert sum(int(item.len) for item in expected) == length


@cocotb.test(timeout_time=20_000_000, timeout_unit="ns")
async def huge_page_and_maximum_message_cover_every_byte(dut):
    tb = TB(dut)
    await tb.reset()
    model = TxReferenceModel()
    length = 16 * 1024 * 1024
    await configure_basic(
        tb,
        model,
        max_len=length,
        mr_length=length,
        page_shift=21,
    )
    submission = DecodedGdrpSubmission(
        qpn=0,
        token=make_token(slot_id=0, length=length, debug_seq=5),
    )
    expected = await submit_and_compare(
        tb, model, submission, event="16MiB", cycles=100_000
    )
    assert len(expected) == 16 * 1024
    assert int(expected[0].psnCount) == _psn_count_pmtu256(length)
    assert sum(int(item.len) for item in expected) == length
    assert int(expected[0].isFirst) == 1
    assert int(expected[-1].isLast) == 1


@cocotb.test()
async def qp_psn_state_is_independent_and_wraps_at_24_bits(dut):
    tb = TB(dut)
    await tb.reset()
    model = TxReferenceModel()
    await configure_basic(tb, model, initial_psn=0xFFFFFE)

    second_qp = QpEntry(valid=1, pmtu=0, initialPsn=100)
    await configure(
        tb.qp_cfg_req,
        tb.qp_cfg_resp,
        QpCfgReq(index=1, entry=second_qp).pack(),
        tb.clock,
    )
    model.configure_qp(1, second_qp)

    submissions = [
        DecodedGdrpSubmission(
            qpn=0, token=make_token(slot_id=0, length=512, debug_seq=0)
        ),
        DecodedGdrpSubmission(
            qpn=1, token=make_token(slot_id=0, length=1, debug_seq=1)
        ),
        DecodedGdrpSubmission(
            qpn=0, token=make_token(slot_id=0, length=1, debug_seq=2)
        ),
    ]
    expected = [model.submit(submission)[0] for submission in submissions]
    # qpn0's 512 B message at pmtu 256 spans 3 PSNs (228 + 244 + 40), so the
    # wrap lands on 0xFFFFFE -> 0xFFFFFF -> 0x000000 and the next grant is 1.
    assert [int(item.startPsn) for item in expected] == [0xFFFFFE, 100, 1]

    for submission, expected_request in zip(submissions, expected, strict=True):
        await tb.submission.enq(submission.pack())
        TxScoreboard.compare(
            expected_request,
            await pipe_get(tb.dma, tb.clock),
            event=f"qp={int(submission.qpn)}",
        )
