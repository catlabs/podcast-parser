import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";

interface Props {
  text: string;
  className?: string;
}

// Renders an assistant answer as Markdown. Safe by construction:
//   - no rehype-raw → raw HTML in the model output is not rendered
//   - only remark-gfm is loaded (headings, lists, ---, **bold**, tables, etc.)
//
// Streaming-friendly: react-markdown re-parses on every render, so partial
// unclosed markers ("**bold") simply render as literal text until the
// closing marker arrives in a later token.
export function MarkdownAnswer({ text, className }: Props) {
  return (
    <div className={className ?? "answer-md"}>
      <ReactMarkdown remarkPlugins={[remarkGfm]}>{text}</ReactMarkdown>
    </div>
  );
}
