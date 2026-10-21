export type AnswerOption = {
  label: string;
  text: string;
};

export function answerText(
  answer: boolean | string | null | undefined,
  options: AnswerOption[] = [],
) {
  if (answer === true) return "正确";
  if (answer === false) return "错误";
  if (answer === null || answer === undefined) return "未作答";
  const option = options.find((item) => item.label === answer);
  return option?.text ? `${answer}．${option.text}` : answer;
}
