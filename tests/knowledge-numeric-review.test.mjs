import assert from "node:assert/strict";
import test from "node:test";
import {
  correctedNumericText,
  freezeNumericCommand,
  numericSelection,
  numericReviewPendingScope,
  parseNumericReview,
  postNumericCommand,
} from "../lib/knowledge-numeric-review.ts";

const passage = {
  passage_id: "fixture-passage",
  document_id: "fixture-document",
  document_version: "v1",
  page_number: 2,
  text: "📐拉力 +123.40 kN",
  source_sha256: "a".repeat(64),
  text_sha256: "b".repeat(64),
};
const correction = {
  start: 4,
  end: 11,
  original_value: "+123.40",
  corrected_value: "+123.400",
  original_unit: "kN",
  corrected_unit: "kN",
  unit_start: 12,
  unit_end: 14,
};
function envelope() {
  return {
    passage_id: passage.passage_id,
    document_id: passage.document_id,
    source_binding: {
      expected_document_version: "v1",
      expected_source_sha256: passage.source_sha256,
      expected_text_sha256: passage.text_sha256,
      expected_parser_version: "docling-test",
      expected_locator_sha256: "c".repeat(64),
    },
    original_text: passage.text,
    page_number: 2,
    current: null,
    history: [],
    next_cursor: null,
    permissions: { can_propose: true, can_approve: false, can_reject: false, can_withdraw: false },
    updates_retrieval_text: false,
    authorizes_work: false,
    fixtureFallback: false,
  };
}

test("pending commands are isolated by real subject and immutable source identity", () => {
  const first = numericReviewPendingScope("alice", passage);
  assert.notEqual(first, numericReviewPendingScope("bob", passage));
  assert.notEqual(
    first,
    numericReviewPendingScope("alice", { ...passage, source_sha256: "d".repeat(64) }),
  );
  assert.equal(first, numericReviewPendingScope("alice", structuredClone(passage)));
  assert.throws(() => numericReviewPendingScope("", passage));
});

test("preview changes explicit source spans without implicit unit conversion or precision loss", () => {
  assert.equal(correctedNumericText(passage.text, [correction]), "📐拉力 +123.400 kN");
  assert.equal(
    correctedNumericText(passage.text, [{ ...correction, corrected_unit: "MN" }]),
    "📐拉力 +123.400 MN",
  );
  assert.throws(
    () =>
      correctedNumericText(passage.text, [
        { ...correction, unit_start: null, unit_end: null, corrected_unit: "MN" },
      ]),
    /隐式换算/,
  );
  assert.throws(() => correctedNumericText(passage.text, [correction, correction]), /重叠/);
  assert.throws(() => correctedNumericText(passage.text, [{ ...correction, start: 5 }]), /不一致/);
  for (const corrected_value of ["NaN", "Infinity", "1e1000", "1,000", " 12"]) {
    assert.throws(
      () => correctedNumericText(passage.text, [{ ...correction, corrected_value }]),
      /十进制/,
    );
  }
  assert.throws(
    () => correctedNumericText(passage.text, [{ ...correction, corrected_unit: "" }]),
    /单位选区/,
  );
});

test("read validation rejects source drift, malformed history and approval by the proposer", () => {
  const valid = envelope();
  assert.equal(parseNumericReview(valid, passage).current, null);
  const approval = {
    review_id: "fixture-review",
    revision: 2,
    status: "approved",
    kind: "correct_values",
    corrections: [correction],
    corrected_text: "📐拉力 +123.400 kN",
    reason: "synthetic comparison",
    proposed_by: "author",
    proposed_at: "2026-10-23T01:00:00Z",
    reviewed_by: "independent-reviewer",
    reviewed_at: "2026-10-23T01:02:00Z",
    review_reason: "synthetic independent check",
  };
  assert.equal(
    parseNumericReview({ ...valid, current: approval }, passage).current.status,
    "approved",
  );
  assert.throws(
    () =>
      parseNumericReview({ ...valid, current: { ...approval, reviewed_by: "author" } }, passage),
    /不匹配/,
  );
  assert.throws(
    () =>
      parseNumericReview(
        {
          ...valid,
          source_binding: { ...valid.source_binding, expected_text_sha256: "d".repeat(64) },
        },
        passage,
      ),
    /不匹配/,
  );
  assert.throws(
    () => parseNumericReview({ ...valid, history: [{ revision: 1 }] }, passage),
    /不完整/,
  );
  assert.throws(
    () =>
      parseNumericReview(
        { ...valid, current: { ...approval, corrected_text: "fabricated text" } },
        passage,
      ),
    /不匹配/,
  );
  assert.throws(() => parseNumericReview({ ...valid, authorizes_work: true }, passage), /不匹配/);
});

test("numeric offsets preserve Chinese and emoji and never split a surrogate pair", () => {
  const source = "📐初值 1.230e+02 kN，复测 −5.0 °C";
  const start = source.indexOf("1.230");
  assert.deepEqual(numericSelection(source, start, start + "1.230e+02".length), {
    start: 4,
    end: 13,
    original: "1.230e+02",
  });
  assert.equal(Array.from(source).slice(4, 13).join(""), "1.230e+02");
  assert.throws(() => numericSelection(source, 1, 2), /Unicode/);
  assert.throws(() => numericSelection(source, 4, 4), /完整数值/);
});

test("unknown writes make one attempt and explicit retry uses frozen body and key", async () => {
  const originalFetch = globalThis.fetch;
  const payload = { corrections: [{ original: "1.00", corrected: "10.0" }] };
  const command = freezeNumericCommand(
    "/api/backend/knowledge/passages/fixture/numeric-review",
    payload,
  );
  payload.corrections[0].corrected = "999";
  const calls = [];
  globalThis.fetch = async (url, init) => {
    calls.push({ url, body: init.body, key: new Headers(init.headers).get("idempotency-key") });
    throw new TypeError("synthetic response lost");
  };
  try {
    await assert.rejects(
      postNumericCommand(command),
      (error) => error.status === 0 && error.operationKey === command.key,
    );
    assert.equal(calls.length, 1);
    await assert.rejects(postNumericCommand(command));
    assert.equal(calls.length, 2);
    assert.deepEqual(calls[0], calls[1]);
    assert.equal(JSON.parse(command.body).corrections[0].corrected, "10.0");
    assert.ok(Object.isFrozen(command));
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test("403, 409 and unreadable success retain the original operation key without retries", async () => {
  const originalFetch = globalThis.fetch;
  const command = freezeNumericCommand("/api/backend/knowledge/passages/fixture/numeric-review", {
    expected_revision: 0,
  });
  try {
    for (const status of [403, 409, 200]) {
      let attempts = 0;
      globalThis.fetch = async () => {
        attempts++;
        return status === 200
          ? new Response("{truncated", { status })
          : Response.json(
              { error: { code: "REJECTED", message: "synthetic refusal" } },
              { status },
            );
      };
      await assert.rejects(
        postNumericCommand(command),
        (error) =>
          error.status === (status === 200 ? 0 : status) && error.operationKey === command.key,
      );
      assert.equal(attempts, 1);
    }
  } finally {
    globalThis.fetch = originalFetch;
  }
});
