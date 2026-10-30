import { Composition } from "remotion";
import { Reel, reelSchema, defaultReelProps } from "./Reel";

// Duración total = suma de las duraciones de clip (en frames). Remotion exige
// un durationInFrames > 0 al registrar; calculateMetadata lo ajusta por props.
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
        const totalSeconds = props.clips.reduce(
          (acc, c) => acc + (c.durationInSeconds || 0),
          0,
        );
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
