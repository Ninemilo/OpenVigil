import type { Metadata } from "next";
import { notFound } from "next/navigation";
import { guideChapters } from "@/lib/user-guide";
import { GuideArticle } from "../guide-article";

type GuidePageProps = { params: Promise<{ chapter: string }> };

function findChapter(slug: string) {
  return guideChapters.find((chapter) => chapter.slug === slug);
}

export async function generateMetadata({ params }: GuidePageProps): Promise<Metadata> {
  const chapter = findChapter((await params).chapter);
  return chapter
    ? { title: `${chapter.title} | 使用指南`, description: chapter.summary }
    : { title: "指南章节未找到" };
}

export default async function GuideChapterPage({ params }: GuidePageProps) {
  const chapter = findChapter((await params).chapter);
  if (!chapter) notFound();
  return <GuideArticle chapter={chapter} />;
}
