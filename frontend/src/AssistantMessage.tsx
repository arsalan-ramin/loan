import ReactMarkdown, { type Components } from 'react-markdown';
import remarkGfm from 'remark-gfm';

type AssistantMessageProps = { content: string };

const components: Components = {
  table: ({ children }) => <div className="assistant-table-scroll"><table>{children}</table></div>,
};

export default function AssistantMessage({ content }: AssistantMessageProps) {
  return <div className="assistant-markdown">
    <ReactMarkdown remarkPlugins={[remarkGfm]} components={components}>{content}</ReactMarkdown>
  </div>;
}
