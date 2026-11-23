#version 450
// the bound texture at the rectangle's uv
layout(set = 0, binding = 0) uniform sampler2D tex;
layout(location = 0) in vec2 uv;
layout(location = 0) out vec4 c;
void main() { c = texture(tex, uv); }
