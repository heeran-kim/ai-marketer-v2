// src/app/(protected)/posts/page.tsx
"use client";

import { useRouter } from "next/navigation";
import { Header } from "@/components/common";
import { PostEditorProvider } from "@/context/PostEditorContext";
import { PostEditorEntry } from "./editor";

import { useFetchData } from "@/hooks/dataHooks";
import { POSTS_API } from "@/constants/api";
import { PostListDto } from "@/types/dto";
import { mapPostDtoToPost } from "@/utils/transformers";

export default function PostsDashboard() {
  const router = useRouter();
  const { data, isLoading, error } = useFetchData<PostListDto>(POSTS_API.LIST);
  const posts = (data?.posts || []).map(mapPostDtoToPost);
  const syncErrors = data?.syncErrors;

  return (
    <PostEditorProvider>
      <Header
        title="Posts"
        actionButton={{
          label: "Create Posts",
          onClick: () => router.push("/posts?mode=create", { scroll: false }),
          isDisabled: !data?.linked || (syncErrors && syncErrors.length > 0),
          tooltipContent: !data?.linked
            ? "You need to link social account first."
            : syncErrors && syncErrors.length > 0
            ? "Please fix social media sync issues before creating new posts."
            : "Create posts for your business. Our AI generates captions for you and helps publish them on linked platforms.",
        }}
      />
      <PostEditorEntry
        posts={posts}
        error={error}
        isLoading={isLoading}
        syncErrors={syncErrors}
      />
    </PostEditorProvider>
  );
}
