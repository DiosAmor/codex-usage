"use strict";

const assert = require("node:assert/strict");
const test = require("node:test");
const daily = require("../static/daily.js");
const account = (rows, status = "ok", error = null) => ({ id: "a", tokenUsage: { status, error, daily: rows } });

test("KST today and recent periods include today across UTC and month boundaries", () => {
  assert.equal(daily.todayKey(Date.parse("2026-10-12T15:01:00Z")), "2026-10-13");
  assert.equal(daily.todayKey(Date.parse("2026-10-12T14:59:59Z")), "2026-10-12");
  const dates = daily.periodDates([], "7", "2026-03-03");
  assert.equal(dates.length, 7);
  assert.equal(dates[0], "2026-02-25");
  assert.equal(dates.at(-1), "2026-03-03");
  assert.equal(daily.periodDates([], "30", "2026-10-13")[0], "2026-09-14");
});

test("server date keys remain dates and invalid calendar dates are excluded", () => {
  assert.equal(daily.validDate("2026-02-29"), false);
  assert.equal(daily.validDate("2024-02-29"), true);
  assert.equal(daily.validDate("0001-01-01"), true);
  assert.equal(daily.validDate("0000-01-01"), false);
  assert.equal(daily.validDate("2026-10-13T00:00:00Z"), false);
  assert.equal(daily.shiftDay("2024-03-01", -1), "2024-02-29");
});

test("all range is the union of supplied selected-account dates up to today", () => {
  const accounts = [account([{ date: "2026-10-01", tokens: "1" }, { date: "2026-10-13", tokens: null }, { date: "9999-01-01", tokens: "2" }, { date: "2026-13-01", tokens: "2" }]), account([{ date: "2026-10-06", tokens: "0" }])];
  assert.deepEqual(daily.periodDates(accounts, "all", "2026-10-13"), ["2026-10-01", "2026-10-06", "2026-10-13"]);
  assert.deepEqual(daily.periodDates([], "all", "2026-10-13"), []);
});

test("int64 values and sums keep exact digits instead of floating point rounding", () => {
  const rows = [{ date: "2026-10-01", tokens: "9007199254740993" }, { date: "2026-10-02", tokens: "9223372036854775807" }];
  const value = daily.summary(account(rows), ["2026-10-01", "2026-10-02"]);
  assert.equal(value.total, 9232379236109516800n);
  assert.equal(daily.exact(value.total), "9,232,379,236,109,516,800");
  assert.equal(value.peak.value, 9223372036854775807n);
  assert.equal(value.complete, true);
  assert.equal(daily.tokens(9007199254740993), null);
  assert.equal(daily.tokens("-1"), null);
  assert.equal(daily.tokens("1.5"), null);
});

test("provided zero, null, and a missing day remain distinct", () => {
  const item = account([{ date: "2026-10-01", tokens: "0" }, { date: "2026-10-02", tokens: null }]);
  const rows = daily.records(item);
  assert.deepEqual(daily.cell(rows, "2026-10-01"), { kind: "known", value: 0n });
  assert.deepEqual(daily.cell(rows, "2026-10-02"), { kind: "unknown", value: null });
  assert.deepEqual(daily.cell(rows, "2026-10-03"), { kind: "missing", value: null });
  const value = daily.summary(item, ["2026-10-01", "2026-10-02", "2026-10-03"]);
  assert.equal(value.total, 0n);
  assert.deepEqual([value.known, value.unknown, value.missing], [1, 1, 1]);
  assert.equal(value.complete, false);
  assert.deepEqual(value.peak, { date: "2026-10-01", value: 0n });
});

test("pending/error data never claims a complete total or peak, and absent data stays unknown", () => {
  const rows = [{ date: "2026-10-01", tokens: "10" }], dates = ["2026-10-01"];
  assert.equal(daily.summary(account(rows, "pending"), dates).complete, false);
  assert.equal(daily.summary(account(rows, "error", "조회 실패"), dates).complete, false);
  assert.equal(daily.summary(account([], "pending"), dates).total, null);
  assert.equal(daily.summary(account([{ date: "2026-10-01", tokens: null }]), dates).peak, null);
});

test("summary respects selected dates and resolves equal peaks to the latest day", () => {
  const item = account([{ date: "2026-09-01", tokens: "999999" }, { date: "2026-10-01", tokens: "123" }, { date: "2026-10-02", tokens: "123" }]);
  const value = daily.summary(item, ["2026-10-01", "2026-10-02"]);
  assert.equal(value.total, 246n);
  assert.deepEqual(value.peak, { date: "2026-10-02", value: 123n });
  assert.equal(daily.compact(123456789n), "1.2억");
  assert.equal(daily.compact(0n), "0");
});
