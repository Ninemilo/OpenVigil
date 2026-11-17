import { ArrowLeft, BookOpen } from "lucide-react";
import type { ReactNode } from "react";
import { GuideLink } from "@/components/layout/guide-link";
import styles from "./guide.module.css";

export default function GuideLayout({ children }: { children: ReactNode }) {
  return (
    <div className={styles.guide}>
      <header className={styles.header}>
        <div className={styles.headerInner}>
          <GuideLink className={styles.brand} href="/guide" aria-label="OpenVigil 使用指南首页">
            <BookOpen size={22} aria-hidden="true" />
            <span>OpenVigil</span>
          </GuideLink>
          <GuideLink className={styles.returnLink} href="/">
            <ArrowLeft size={16} aria-hidden="true" />
            返回工作台
          </GuideLink>
        </div>
      </header>
      {children}
    </div>
  );
}
