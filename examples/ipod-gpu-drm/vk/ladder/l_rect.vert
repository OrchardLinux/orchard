#version 450
// an axis-aligned rectangle (NDC x0, y0, x1, y1) at depth z.x from the push constants; the vertex buffer holds the
// six corners in [0, 1] (two triangles), passed on as uv scaled by z.yz
layout(push_constant) uniform PC { vec4 rect; vec4 color; vec4 z; } pc;
layout(location = 0) in vec2 t;
layout(location = 0) out vec2 uv;
void main() {
   uv = t * pc.z.yz;
   gl_Position = vec4(mix(pc.rect.xy, pc.rect.zw, t), pc.z.x, 1.0);
}
