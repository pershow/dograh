import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";

import styles from "./workbench.module.css";

export function WorkbenchMarkdown({ children }: { children: string }) {
  return (
    <div className={styles.markdown}>
      <ReactMarkdown
        remarkPlugins={[remarkGfm]}
        skipHtml
        components={{
          table: ({ children }) => (
            <div className={styles.markdownTable} role="region" aria-label="表格" tabIndex={0}>
              <table>{children}</table>
            </div>
          ),
          a: ({ children, href }) => <a href={href} target="_blank" rel="noopener noreferrer">{children}</a>,
        }}
      >
        {children}
      </ReactMarkdown>
    </div>
  );
}
