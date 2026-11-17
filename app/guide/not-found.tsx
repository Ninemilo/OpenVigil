import { GuideLink } from "@/components/layout/guide-link";
import styles from "./guide.module.css";

export default function GuideNotFound() {
  return (
    <main className={styles.missing}>
      <h1>指南章节未找到</h1>
      <p>请从使用指南首页选择要查阅的章节。</p>
      <GuideLink className={styles.inlineLink} href="/guide">
        返回使用指南
      </GuideLink>
    </main>
  );
}
