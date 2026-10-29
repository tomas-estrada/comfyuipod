import React from "react";
import {
  AbsoluteFill,
  Audio,
  Img,
  OffthreadVideo,
  Sequence,
  interpolate,
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
});

export const reelSchema = z.object({
  fps: z.number().default(30),
  width: z.number().default(1080),
  height: z.number().default(1920),
  audio: z.string().nullable().default(null),
  clips: z.array(clipSchema).default([]),
});

export type ReelProps = z.infer<typeof reelSchema>;

export const defaultReelProps: ReelProps = {
  fps: 30,
  width: 1080,
  height: 1920,
  audio: null,
  clips: [],
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
        <OffthreadVideo src={src} style={{ width: "100%", height: "100%", objectFit: "cover" }} />
      ) : (
        <Img src={src} style={{ width: "100%", height: "100%", objectFit: "cover" }} />
      )}
    </AbsoluteFill>
  );
};

export const Reel: React.FC<ReelProps> = ({ audio, clips }) => {
  const { fps } = useVideoConfig();
  const transitionFrames = Math.round(0.3 * fps);
  let cursor = 0;
  return (
    <AbsoluteFill style={{ backgroundColor: "black" }}>
      {clips.map((clip, i) => {
        const durationInFrames = Math.max(1, Math.round(clip.durationInSeconds * fps));
        const from = cursor;
        cursor += durationInFrames;
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
      {audio ? <Audio src={audio} /> : null}
    </AbsoluteFill>
  );
};
