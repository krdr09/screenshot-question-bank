import { test, expect } from "@playwright/test";

import { assertPayloadsHaveNoAnswerLeakage } from "../support/ui";

test("提交前响应泄露扫描拒绝隐藏的答案或解析字段", () => {
  expect(() =>
    assertPayloadsHaveNoAnswerLeakage(
      [{ id: "item-1", saved: true, correct_answer: "E" }],
      ["correct_answer", "explanation"],
    ),
  ).toThrow(/correct_answer/);
  expect(() =>
    assertPayloadsHaveNoAnswerLeakage(
      [{ id: "item-1", saved: true, selected_answer: "E" }],
      ["correct_answer", "explanation"],
    ),
  ).not.toThrow();
});
