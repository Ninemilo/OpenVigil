export type UserGuideScreenshot = {
  src: string;
  width: number;
  height: number;
  alt: string;
  callouts: string[];
};

type UserGuideLink = { href: string; label: string };

type UserGuideStep =
  string | { text: string; screenshots: UserGuideScreenshot[]; link?: UserGuideLink };

export type UserGuideSection = {
  id: string;
  title: string;
  paragraphs?: string[];
  steps?: UserGuideStep[];
  bullets?: string[];
  link?: UserGuideLink;
};

export type UserGuideChapter = {
  slug: string;
  title: string;
  summary: string;
  sections: UserGuideSection[];
};
