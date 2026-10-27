/** Plain-text presentation only: never render model output as HTML. */
export default function ClaudeExplanationText({ text }: { text: string }) {
  const sections = text.split(/(?=\b(?:Result|Why|What is unresolved|Next steps):)/i)
    .map((part) => part.trim()).filter(Boolean);
  return <div className="claude-analysis claude-bullets">
    {sections.map((section, index) => {
      const match = /^(Result|Why|What is unresolved|Next steps):\s*([\s\S]*)$/i.exec(section);
      const content = match ? match[2] : section;
      const items = content.split(/\r?\n+|\s+(?=\(\d+\)\s)/)
        .map((item) => item.replace(/^\s*(?:[-*•]\s+|\(\d+\)\s*)/, '').trim()).filter(Boolean);
      return <section key={index}>
        {match && <h5>{match[1]}</h5>}
        <ul>{items.map((item, itemIndex) => <li key={itemIndex}>{item}</li>)}</ul>
      </section>;
    })}
  </div>;
}