#version 450
// the push-constant colour
layout(push_constant) uniform PC { vec4 rect; vec4 color; vec4 z; } pc;
layout(location = 0) out vec4 c;
void main() { c = pc.color; }
