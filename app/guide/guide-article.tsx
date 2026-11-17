import { ArrowLeft, ArrowRight, ChevronDown, List } from "lucide-react";
import Image from "next/image";
import { GuideLink } from "@/components/layout/guide-link";
import { guideChapters, guideGroups } from "@/lib/user-guide";
import type { UserGuideChapter, UserGuideScreenshot } from "@/lib/user-guide/types";
import styles from "./guide.module.css";

function chapterHref(slug: string) {
  return slug ? `/guide/${slug}` : "/guide";
}

function ChapterNav({ activeSlug }: { activeSlug: string }) {
  return (
    <nav aria-label="指南章节" className={styles.chapterNav}>
      {guideGroups.map((group) => (
        <div key={group.label} className={styles.chapterGroup}>
          <p>{group.label}</p>
          <ul>
            {group.chapters.map(({ slug, title }) => (
              <li key={slug}>
                <GuideLink
                  href={chapterHref(slug)}
                  aria-current={activeSlug === slug ? "page" : undefined}
                >
                  {title}
                </GuideLink>
              </li>
            ))}
          </ul>
        </div>
      ))}
    </nav>
  );
}

function PageOutline({
  sections,
  mobile = false,
}: {
  sections: UserGuideChapter["sections"];
  mobile?: boolean;
}) {
  return (
    <nav aria-label="本页目录" className={mobile ? styles.mobileOutline : styles.outline}>
      <p>本页目录</p>
      <ul>
        {sections.map(({ id, title }) => (
          <li key={id}>
            <a href={`#${id}`}>{title}</a>
          </li>
        ))}
      </ul>
    </nav>
  );
}

function GuideScreenshot({ screenshot }: { screenshot: UserGuideScreenshot }) {
  const { src, width, height, alt, callouts } = screenshot;
  return (
    <figure className={styles.screenshot}>
      <a href={src} target="_blank" rel="noreferrer">
        <Image src={src} width={width} height={height} alt={alt} unoptimized loading="lazy" />
        <span>在新标签页查看大图</span>
      </a>
      <figcaption>
        <ol aria-label="截图标注">
          {callouts.map((callout) => (
            <li key={callout}>{callout}</li>
          ))}
        </ol>
      </figcaption>
    </figure>
  );
}

export function GuideArticle({ chapter }: { chapter: UserGuideChapter }) {
  const index = guideChapters.indexOf(chapter);
  const previous = guideChapters[index - 1];
  const next = guideChapters[index + 1];

  return (
    <div className={styles.grid}>
      <aside className={styles.sidebar} aria-label="指南导航">
        <ChapterNav activeSlug={chapter.slug} />
      </aside>
      <details className={styles.mobileChapters}>
        <summary>
          <List size={16} aria-hidden="true" />
          指南章节
          <ChevronDown className={styles.disclosureIcon} size={16} aria-hidden="true" />
        </summary>
        <ChapterNav activeSlug={chapter.slug} />
      </details>
      <main className={styles.main}>
        <article aria-labelledby="guide-title">
          <header className={styles.articleHeader}>
            {chapter.slug ? (
              <GuideLink className={styles.breadcrumb} href="/guide">
                使用指南
              </GuideLink>
            ) : null}
            <h1 id="guide-title">{chapter.title}</h1>
            <p>{chapter.summary}</p>
          </header>
          <PageOutline sections={chapter.sections} mobile />
          {chapter.sections.map(({ id, title, paragraphs, steps, bullets, link }) => (
            <section className={styles.section} key={id} aria-labelledby={id}>
              <h2 id={id}>{title}</h2>
              {paragraphs?.map((paragraph) => (
                <p key={paragraph}>{paragraph}</p>
              ))}
              {steps ? (
                <ol>
                  {steps.map((step) => {
                    if (typeof step === "string") return <li key={step}>{step}</li>;
                    return (
                      <li key={step.text}>
                        {step.text}
                        {step.screenshots.map((screenshot) => (
                          <GuideScreenshot key={screenshot.src} screenshot={screenshot} />
                        ))}
                        {step.link ? (
                          <GuideLink className={styles.inlineLink} href={step.link.href}>
                            {step.link.label} <ArrowRight size={16} aria-hidden="true" />
                          </GuideLink>
                        ) : null}
                      </li>
                    );
                  })}
                </ol>
              ) : null}
              {bullets ? (
                <ul>
                  {bullets.map((bullet) => (
                    <li key={bullet}>{bullet}</li>
                  ))}
                </ul>
              ) : null}
              {link ? (
                <GuideLink className={styles.inlineLink} href={link.href}>
                  {link.label} <ArrowRight size={16} aria-hidden="true" />
                </GuideLink>
              ) : null}
            </section>
          ))}
        </article>
        <nav aria-label="相邻章节" className={styles.pagination}>
          {previous ? (
            <GuideLink href={chapterHref(previous.slug)} rel="prev">
              <ArrowLeft size={17} aria-hidden="true" />
              <span>
                <small>上一章</small>
                {previous.title}
              </span>
            </GuideLink>
          ) : null}
          {next ? (
            <GuideLink href={chapterHref(next.slug)} rel="next">
              <span>
                <small>下一章</small>
                {next.title}
              </span>
              <ArrowRight size={17} aria-hidden="true" />
            </GuideLink>
          ) : null}
        </nav>
      </main>
      <PageOutline sections={chapter.sections} />
    </div>
  );
}
