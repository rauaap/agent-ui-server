import type { ExtensionContext, AgentToolUpdateCallback } from "@earendil-works/pi-coding-agent";
import { Type, type Static } from "typebox";
import { callApiStream, getConfig, applyCitations } from "./api.ts";
import { getModel, missingConfigResult, errorResult, formatResult } from "./utils.ts";

export const UrlContextSchema = Type.Object({
    query: Type.String({ description: "Question or task to perform on the URLs" }),
    urls: Type.Array(Type.String(), { 
        description: "Public URLs to analyze (web pages, documents, images, YouTube videos, etc).",
        minItems: 1,
        maxItems: 20
    }),
});
export type UrlContextInput = Static<typeof UrlContextSchema>;

const YOUTUBE_REGEX = /^(?:https?:\/\/)?(?:www\.)?(?:youtube\.com\/(?:watch\?v=|embed\/)|youtu\.be\/)([a-zA-Z0-9_-]{11})/;

export async function urlContext(
    id: string, 
    params: UrlContextInput, 
    signal: AbortSignal, 
    onUpdate: AgentToolUpdateCallback | undefined, 
    ctx: ExtensionContext
) {
    const model = await getModel(ctx);
    if (!model) return missingConfigResult(ctx);

    const count = params.urls.length;
    onUpdate?.({ content: [{ type: "text", text: `Analyzing ${count} URL${count > 1 ? 's' : ''}...` }], details: {} });

    try {
        const config = getConfig(model);
        if (config.kind !== "google") {
            return formatResult(
                `url_context currently requires a Google Gemini-compatible model. Current model: ${model.id} (${model.provider}/${model.api}).\n\nUse web_search for cross-provider web search, or switch to Gemini for provider-native URL context retrieval.`,
                {
                    error: "unsupported_provider",
                    providerKind: config.kind,
                    model: model.id,
                    supportedProviders: ["google", "google-generative-ai"],
                    grounded: false,
                }
            );
        }
        
        let contents: any[] = [];
        let tools: any[] | undefined = [{ [config.urlContextTool!]: {} }];

        // Special handling for YouTube videos on Gemini
        if (model.api === "google-generative-ai") {
            const youtubeUrls: string[] = [];
            const otherUrls: string[] = [];

            for (const url of params.urls) {
                if (YOUTUBE_REGEX.test(url)) {
                    youtubeUrls.push(url);
                } else {
                    otherUrls.push(url);
                }
            }

            // If we have YouTube URLs, construct file_data parts
            if (youtubeUrls.length > 0) {
                const parts: any[] = [];
                
                for (const url of youtubeUrls) {
                    parts.push({
                        file_data: { file_uri: url, mime_type: "video/mp4" }
                    });
                }

                let prompt = params.query;
                if (otherUrls.length > 0) {
                    prompt += `\n\nURLs:\n${otherUrls.join("\n")}`;
                } else {
                    // If no other URLs, we might not need the tool, but keep it just in case
                    // or maybe the tool is required for grounding even with video?
                    // "google_search_retrieval" tool might confuse if there are no URLs to retrieve.
                    // But if we remove the tool, we might lose grounding capabilities (like search).
                    // Let's keep the tool enabled.
                }

                parts.push({ text: prompt });
                contents = [{ role: "user", parts }];
            } else {
                // No YouTube URLs, standard behavior
                const combinedPrompt = `${params.query}\n\nURLs:\n${params.urls.join("\n")}`;
                contents = [{ role: "user", parts: [{ text: combinedPrompt }] }];
            }
        } else {
            // Not Gemini, standard behavior
            const combinedPrompt = `${params.query}\n\nURLs:\n${params.urls.join("\n")}`;
            contents = [{ role: "user", parts: [{ text: combinedPrompt }] }];
        }

        const result = await callApiStream(ctx, model, {
            contents,
            ...(tools ? { tools } : {})
        }, onUpdate, signal);

        const cited = applyCitations(result.text, result.groundingMetadata);
        const text = cited.text;
        const sources = result.sources?.length ? result.sources : cited.sources;
        const extraSearchResults = (result.searchResults || []).filter((item) => item.url && !sources.some((source) => source.url === item.url));
        
        // Handle both camelCase and snake_case metadata
        const urlMeta = result.urlContextMetadata?.urlMetadata 
            || result.urlContextMetadata?.url_metadata || [];

        const retrieved = urlMeta
            .filter((m: any) => (m.urlRetrievalStatus || m.url_retrieval_status) === "URL_RETRIEVAL_STATUS_SUCCESS")
            .map((m: any) => m.retrievedUrl || m.retrieved_url || m.url);

        const failed = urlMeta
            .filter((m: any) => (m.urlRetrievalStatus || m.url_retrieval_status) !== "URL_RETRIEVAL_STATUS_SUCCESS")
            .map((m: any) => ({ 
                url: m.retrievedUrl || m.retrieved_url || m.url, 
                status: m.urlRetrievalStatus || m.url_retrieval_status 
            }));

        let summary = text;
        if (failed.length > 0) {
            summary += `\n\n## URL Status\n✅ Retrieved: ${retrieved.length}\n❌ Failed: ${failed.length}`;
            failed.forEach((f: any) => { summary += `\n- ${f.url}: ${f.status}`; });
        }
        const hasUrlContextMetadata = urlMeta.length > 0 || retrieved.length > 0 || sources.length > 0 || extraSearchResults.length > 0;
        if (!hasUrlContextMetadata) {
            summary += `\n\n## URL Context Verification\n⚠️ No verified URL context metadata was returned by provider ${result.providerKind || "unknown"}. Treat the answer as ungrounded unless sources, retrieved URLs, or searchResults are present in tool details.`;
        }
        if (sources.length > 0 && !summary.includes("## Sources")) {
            summary += `\n\n## Sources\n${sources.map((s, i) => `${i + 1}. [${s.title}](${s.url})`).join("\n")}`;
        }
        if (extraSearchResults.length) {
            const visibleResults = extraSearchResults.slice(0, 8);
            summary += `\n\n## Additional Search Results\n${visibleResults.map((r, i) => {
                const label = r.title || r.url || `Result ${i + 1}`;
                const url = r.url ? ` - ${r.url}` : "";
                const meta = [r.source, r.type, r.status, r.query ? `query=${r.query}` : undefined].filter(Boolean).join(", ");
                return `${i + 1}. ${label}${url}${meta ? ` (${meta})` : ""}`;
            }).join("\n")}`;
            if (extraSearchResults.length > visibleResults.length) {
                summary += `\n... and ${extraSearchResults.length - visibleResults.length} more results in tool details.`;
            }
        }

        return formatResult(summary, {
            sources,
            providerKind: result.providerKind,
            nativeSearchUsed: result.nativeSearchUsed,
            nativeSearchEvents: result.nativeSearchEvents,
            nativeSearchCalls: result.nativeSearchCalls,
            searchQueries: result.searchQueries,
            searchResults: result.searchResults,
            citations: result.citations,
            retrieved,
            failed: failed.length > 0 ? failed : undefined,
            model: model.id,
            grounded: sources.length > 0 || (result.searchResults?.length || 0) > 0,
            resultCount: result.searchResults?.length || sources.length
        });
    } catch (e: any) {
        return errorResult(e);
    }
}
