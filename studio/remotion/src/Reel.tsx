import React from "react";
import {
  AbsoluteFill,
  Audio,
  Img,
  OffthreadVideo,
  Sequence,
  interpolate,
  staticFile,
  useCurrentFrame,
  useVideoConfig,
} from "remotion";
import { z } from "zod";

// Esquema de las input-props que construye EditorEngine._remotion_props (Python).
// Debe seguir siendo un espejo exacto de esa función.
const clipSchema = z.object({
  src: z.string(),
  durationInSeconds: z.number(),
  transitionIn: z.enum(["cut", "crossfade", "fade", "slide"]).default("cut"),
  transitionDurationSeconds: z.number().min(0).max(5).default(0.3),
});

const overlaySchema = z.object({
  text: z.string().min(1).max(1000),
  startInSeconds: z.number().min(0),
  endInSeconds: z.number().positive(),
  position: z.enum(["top", "center", "bottom"]).default("bottom"),
});

export const reelSchema = z.object({
  fps: z.number().default(30),
  width: z.number().default(1080),
  height: z.number().default(1920),
  audio: z.string().nullable().default(null),
  clips: z.array(clipSchema).default([]),
  titles: z.array(overlaySchema).default([]),
  captions: z.array(overlaySchema).default([]),
});

export type ReelProps = z.infer<typeof reelSchema>;

export const defaultReelProps: ReelProps = {
  fps: 30,
  width: 1080,
  height: 1920,
  audio: null,
  clips: [],
  titles: [],
  captions: [],
};

const isVideo = (src: string) => /\.(mp4|mov|webm|mkv|m4v)$/i.test(src);

const Clip: React.FC<{
  src: string;
  transitionIn: ReelProps["clips"][number]["transitionIn"];
  transitionFrames: number;
}> = ({ src, transitionIn, transitionFrames }) => {
  const frame = useCurrentFrame();
  const fade =
    transitionIn === "cut" || transitionFrames <= 0
      ? 1
      : interpolate(frame, [0, transitionFrames], [0, 1], {
          extrapolateLeft: "clamp",
          extrapolateRight: "clamp",
        });
  const slideX =
    transitionIn === "slide"
      ? interpolate(frame, [0, transitionFrames], [100, 0], {
          extrapolateLeft: "clamp",
          extrapolateRight: "clamp",
        })
      : 0;
  const opacity = transitionIn === "slide" ? 1 : fade;
  return (
    <AbsoluteFill
      style={{
        opacity,
        transform: `translateX(${slideX}%)`,
        backgroundColor: "black",
      }}
    >
      {isVideo(src) ? (
        <OffthreadVideo
          src={staticFile(src)}
          style={{ width: "100%", height: "100%", objectFit: "cover" }}
        />
      ) : (
        <Img
          src={staticFile(src)}
          style={{ width: "100%", height: "100%", objectFit: "cover" }}
        />
      )}
    </AbsoluteFill>
  );
};

export const Reel: React.FC<ReelProps> = ({ audio, clips, titles, captions }) => {
  const { fps, width } = useVideoConfig();
  let cursor = 0;
  let previousDurationFrames = 0;
  return (
    <AbsoluteFill style={{ backgroundColor: "black" }}>
      {clips.map((clip, i) => {
        const durationInFrames = Math.max(1, Math.round(clip.durationInSeconds * fps));
        const transitionFrames = Math.max(
          0,
          Math.round(clip.transitionDurationSeconds * fps),
        );
        const overlapFrames =
          i > 0 && (clip.transitionIn === "crossfade" || clip.transitionIn === "slide")
            ? Math.min(transitionFrames, durationInFrames, previousDurationFrames)
            : 0;
        const from = Math.max(0, cursor - overlapFrames);
        cursor = from + durationInFrames;
        previousDurationFrames = durationInFrames;
        return (
          <Sequence key={i} from={from} durationInFrames={durationInFrames}>
            <Clip
              src={clip.src}
              transitionIn={clip.transitionIn}
              transitionFrames={transitionFrames}
            />
          </Sequence>
        );
      })}
      {titles.map((overlay, i) => (
        <TextOverlay
          key={`title-${i}`}
          {...overlay}
          kind="title"
          fps={fps}
          width={width}
        />
      ))}
      {captions.map((overlay, i) => (
        <TextOverlay
          key={`caption-${i}`}
          {...overlay}
          kind="caption"
          fps={fps}
          width={width}
        />
      ))}
      {audio ? <Audio src={staticFile(audio)} /> : null}
    </AbsoluteFill>
  );
};

type Overlay = ReelProps["titles"][number];

const TextOverlay: React.FC<
  Overlay & { kind: "title" | "caption"; fps: number; width: number }
> = ({ text, startInSeconds, endInSeconds, position, kind, fps, width }) => {
  const from = Math.round(startInSeconds * fps);
  const duration = Math.max(1, Math.round((endInSeconds - startInSeconds) * fps));
  const justifyContent =
    position === "top" ? "flex-start" : position === "center" ? "center" : "flex-end";
  return (
    <Sequence from={from} durationInFrames={duration} premountFor={0}>
      <AbsoluteFill
        style={{
          alignItems: "center",
          justifyContent,
          padding: "8% 6%",
          boxSizing: "border-box",
          pointerEvents: "none",
        }}
      >
        <div
          style={{
            maxWidth: "100%",
            padding: kind === "caption" ? "0.35em 0.65em" : "0.15em 0.25em",
            borderRadius: kind === "caption" ? 12 : 0,
            backgroundColor: kind === "caption" ? "rgba(0,0,0,0.72)" : "transparent",
            color: "white",
            fontFamily: "Arial, Helvetica, sans-serif",
            fontSize: Math.round(width * (kind === "title" ? 0.075 : 0.042)),
            fontWeight: kind === "title" ? 700 : 600,
            lineHeight: 1.15,
            textAlign: "center",
            whiteSpace: "pre-wrap",
            overflowWrap: "anywhere",
            textShadow: kind === "title" ? "0 2px 12px rgba(0,0,0,0.85)" : "none",
          }}
        >
          {text}
        </div>
      </AbsoluteFill>
    </Sequence>
  );
};
