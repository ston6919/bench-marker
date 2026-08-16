/**
 * Default placeholder composition. Each benchmark run overwrites this file
 * with model-generated Remotion code before rendering.
 */
import React from "react";
import { AbsoluteFill, interpolate, useCurrentFrame } from "remotion";

export const FPS = 30;
export const WIDTH = 960;
export const HEIGHT = 540;
export const DURATION_IN_FRAMES = 90; // 3 seconds

export const MotionGraphic: React.FC = () => {
  const frame = useCurrentFrame();
  const opacity = interpolate(frame, [0, 20], [0, 1], {
    extrapolateRight: "clamp",
  });

  return (
    <AbsoluteFill
      style={{
        backgroundColor: "#0f172a",
        justifyContent: "center",
        alignItems: "center",
        fontFamily: "Inter, system-ui, sans-serif",
      }}
    >
      <div style={{ color: "white", fontSize: 48, opacity, fontWeight: 700 }}>
        Bench Marker
      </div>
    </AbsoluteFill>
  );
};
