import { cdp } from "./cdp.js";
import { BridgeError, sleep } from "./errors.js";

async function evaluate(tabId, expression) {
  const r = await cdp(tabId, "Runtime.evaluate", {
    expression,
    returnByValue: true,
    awaitPromise: true,
  });
  if (r.exceptionDetails)
    throw new BridgeError(
      "failed",
      r.exceptionDetails.exception?.description ?? r.exceptionDetails.text,
    );
  return r.result.value;
}
function markdown() {
  const walk = (n) => {
    if (n.nodeType === Node.TEXT_NODE) return n.textContent;
    if (
      n.nodeType !== Node.ELEMENT_NODE ||
      ["SCRIPT", "STYLE", "NOSCRIPT"].includes(n.tagName)
    )
      return "";
    const body = [...n.childNodes].map(walk).join("");
    if (/^H[1-6]$/.test(n.tagName))
      return (
        "\n" + "#".repeat(Number(n.tagName[1])) + " " + body.trim() + "\n\n"
      );
    switch (n.tagName) {
      case "A":
        return `[${body}](${n.href})`;
      case "IMG":
        return `![${n.alt}](${n.src})`;
      case "STRONG":
      case "B":
        return `**${body}**`;
      case "EM":
      case "I":
        return `*${body}*`;
      case "PRE":
        return "\n```\n" + n.textContent + "\n```\n";
      case "CODE":
        return "`" + body + "`";
      case "LI":
        return "- " + body.trim() + "\n";
      case "BR":
        return "\n";
      case "P":
      case "DIV":
      case "SECTION":
        return body + "\n\n";
      default:
        return body;
    }
  };
  return walk(document.body)
    .replace(/\n{3,}/g, "\n\n")
    .trim();
}
export async function exportContent(tabId, p) {
  const tab = await chrome.tabs.get(tabId),
    url = new URL(tab.url),
    format = p.format ?? "text";
  if (
    ["xlsx", "csv", "docx", "pptx"].includes(format) ||
    (p.gsuite && ["pdf", "md"].includes(format))
  ) {
    const match = /^\/(document|spreadsheets|presentation)\/d\/([\w-]+)/.exec(
      url.pathname,
    );
    if (url.hostname !== "docs.google.com" || !match)
      throw new BridgeError(
        "invalid_argument",
        "This export format requires an open Google Workspace document",
      );
    const formats = {
      document: ["docx", "pdf", "md"],
      spreadsheets: ["xlsx", "csv", "pdf"],
      presentation: ["pptx", "pdf"],
    };
    if (!formats[match[1]].includes(format))
      throw new BridgeError(
        "invalid_argument",
        "Format is not supported for this document type",
      );
    const downloadUrl =
      `https://docs.google.com/${match[1]}/d/${match[2]}/export` +
      (match[1] === "presentation" ? `/${format}` : `?format=${format}`);
    const id = await chrome.downloads.download({
      url: downloadUrl,
      saveAs: false,
      conflictAction: "uniquify",
    });
    const deadline = Date.now() + 45000;
    while (Date.now() < deadline) {
      const [download] = await chrome.downloads.search({ id });
      if (download?.state === "complete") return { download, format };
      if (download?.state === "interrupted")
        throw new BridgeError(
          "failed",
          `Export interrupted: ${download.error}`,
        );
      await sleep(100);
    }
    return { download_id: id, state: "in_progress", format };
  }
  if (format === "pdf") {
    const result = await cdp(tabId, "Page.printToPDF", {
      printBackground: true,
    });
    return {
      content_base64: result.data,
      mime_type: "application/pdf",
      format,
    };
  }
  if (format === "youtube_transcript") {
    if (
      !["youtube.com", "www.youtube.com"].includes(url.hostname) ||
      url.pathname !== "/watch"
    )
      throw new BridgeError(
        "invalid_argument",
        "Transcript export requires a YouTube watch page",
      );
    const content = await evaluate(
      tabId,
      `(async()=>{
      const visible=[...document.querySelectorAll('ytd-transcript-segment-renderer')].map(x=>x.innerText).join('\\n');if(visible)return visible;
      const tracks=window.ytInitialPlayerResponse?.captions?.playerCaptionsTracklistRenderer?.captionTracks;
      if(!tracks?.length)throw Error('No captions available; open the transcript in the page and retry');
      const response=await fetch(tracks[0].baseUrl+'&fmt=json3');if(!response.ok)throw Error('Caption fetch failed');
      const data=await response.json();return (data.events??[]).map(e=>(e.segs??[]).map(s=>s.utf8).join('')).filter(Boolean).join('\\n');
    })()`,
    );
    return { content, format: "text", url: tab.url };
  }
  if (!["text", "html", "md"].includes(format))
    throw new BridgeError("invalid_argument", "Unknown export format");
  const expression =
    format === "html"
      ? `(()=>{const h=document.getElementById("second-cursor-overlay"),html=document.documentElement.outerHTML;return h?html.replace(h.outerHTML,""):html})()`
      : format === "md"
        ? `(${markdown.toString()})()`
        : "document.body.innerText";
  return { content: await evaluate(tabId, expression), format, url: tab.url };
}
