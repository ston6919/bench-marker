import React from "react";
import { Composition } from "remotion";
import { MotionGraphic, DURATION_IN_FRAMES, FPS, WIDTH, HEIGHT } from "./MotionGraphic";

export const RemotionRoot: React.FC = () => {
  return (
    <>
      <Composition
        id="MotionGraphic"
        component={MotionGraphic}
        durationInFrames={DURATION_IN_FRAMES}
        fps={FPS}
        width={WIDTH}
        height={HEIGHT}
      />
    </>
  );
};
