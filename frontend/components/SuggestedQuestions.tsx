export default function SuggestedQuestions({
  questions,
  onPick,
}: {
  questions: string[];
  onPick: (q: string) => void;
}) {
  if (!questions.length) return null;
  return (
    <div className="mt-2 flex flex-wrap gap-1.5">
      {questions.map((q, i) => (
        <button
          key={i}
          onClick={() => onPick(q)}
          className="rounded-full border border-border px-3 py-1 text-xs text-muted transition-colors hover:border-accent hover:bg-accent/10 hover:text-text"
        >
          {q}
        </button>
      ))}
    </div>
  );
}
