import { Composition } from "remotion";
import { Reel, reelSchema, defaultReelProps } from "./Reel";

// Duración total con solapamiento para crossfade/slide, como en Reel.
export const RemotionRoot: React.FC = () => {
  return (
    <Composition
      id="Reel"
      component={Reel}
      schema={reelSchema}
      defaultProps={defaultReelProps}
      fps={30}
      width={1080}
      height={1920}
      durationInFrames={30}
      calculateMetadata={({ props }) => {
        const fps = props.fps ?? 30;
        let totalSeconds = 0;
        let previousDuration = 0;
        props.clips.forEach((clip, index) => {
          const duration = clip.durationInSeconds || 0;
          const overlap =
            index > 0 && (clip.transitionIn === "crossfade" || clip.transitionIn === "slide")
              ? Math.min(clip.transitionDurationSeconds ?? 0.3, duration, previousDuration)
              : 0;
          totalSeconds += duration - overlap;
          previousDuration = duration;
        });
        return {
          durationInFrames: Math.max(1, Math.round(totalSeconds * fps)),
          fps,
          width: props.width ?? 1080,
          height: props.height ?? 1920,
        };
      }}
    />
  );
};
