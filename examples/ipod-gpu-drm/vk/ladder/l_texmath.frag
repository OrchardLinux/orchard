#version 450
// two samples whose results feed arithmetic: the average of the texel and its right neighbour (a 1/64 step)
layout(set = 0, binding = 0) uniform sampler2D tex;
layout(location = 0) in vec2 uv;
layout(location = 0) out vec4 c;
void main() { c = 0.5 * texture(tex, uv) + 0.5 * texture(tex, uv + vec2(1.0 / 64.0, 0.0)); }
