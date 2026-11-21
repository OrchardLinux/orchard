/* SPDX-License-Identifier: MIT */
/*
 * a8-vk-ladder <rung>|list: a Vulkan escalation suite for Mesa on the A8 (powervr T7000 variant), one process per
 * rung, run with the /opt/a8vk bundle's libraries (see ../README.md). Every pixel or value is checked on the
 * CPU. Prints exactly one `RESULT <rung> PASS|FAIL|TIMEOUT|SKIP <detail>` line; exit 0 PASS/SKIP, 1 FAIL, 3 TIMEOUT.
 * Each submission waits at most A8_LADDER_TIMEOUT_MS (default 5000) on its fence; on expiry the rung prints TIMEOUT
 * and leaves with _exit() without tearing anything down (the kernel reclaims the context with the process).
 * A8_SMOKE_NOCHECK=1 skips the result checks (e.g. on a fake DRM device, where nothing executes).
 * Self-reporting: before every step that enters the kernel driver (instance, device, each submit, each fence wait,
 * teardown) it prints `STEP <rung> +<ms> <what>` and flushes, so a hang shows where it stopped.
 * A8_LADDER_TEARDOWN=1: after the RESULT line, destroy the device and instance explicitly (each with its STEP line)
 * instead of leaving everything to the file's release at exit.
 *
 * Render targets are B8G8R8A8_UNORM with two mip levels (the twiddled layout the A8 drew first); level 0 is drawn and
 * copied to a host buffer with vkCmdCopyImageToBuffer. Rectangles are aligned to pixel boundaries, so every pixel centre
 * is strictly inside or outside; for triangles the pixels within 0.75 px of an edge are not judged.
 */
#include <math.h>
#include <stdarg.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <unistd.h>
#include <vulkan/vulkan.h>

#define MAX2(a, b) ((a) > (b) ? (a) : (b))

#include "l_big_comp.h"
#include "l_const_frag.h"
#include "l_pc_frag.h"
#include "l_rect_vert.h"
#include "l_tex_frag.h"
#include "l_tex2_frag.h"
#include "l_texmath_frag.h"
#include "l_lit_vert.h"
#include "l_tri_vert.h"
#include "l_ubo_frag.h"
#include "l_vcol_frag.h"
#include "l_vcol_vert.h"
#include "l_verts_comp.h"

static const char *rung;
static int nocheck;
static uint64_t timeout_ns = 5000000000ull;
static VkInstance inst;
static VkPhysicalDevice phy;
static VkDevice dev;
static VkQueue q;
static VkCommandPool pool;
static VkPhysicalDeviceMemoryProperties memprops;
static double last_ms, sum_ms, max_ms, t_start;
static unsigned njobs;
static int teardown, timed_out;
static int skip_refusal; /* a refusal by Mesa is this rung's SKIP (a feature the A8 driver does not build) */

static double now_ms(void);
static void step(const char *fmt, ...) __attribute__((format(printf, 1, 2)));
static void step(const char *fmt, ...)
{
   va_list ap;
   va_start(ap, fmt);
   printf("STEP %s +%.1f ", rung ? rung : "-", now_ms() - t_start);
   vprintf(fmt, ap);
   printf("\n");
   va_end(ap);
   fflush(stdout);
}

static void finish(int code)
{
   fflush(stdout);
   if (teardown && dev && !timed_out) {
      step("vkDeviceWaitIdle");
      vkDeviceWaitIdle(dev);
      step("vkDestroyDevice");
      vkDestroyDevice(dev, NULL);
      step("vkDestroyInstance");
      vkDestroyInstance(inst, NULL);
      step("teardown done");
   }
   step("exit %d", code);
   _exit(code);
}

#define CK(x)                                                                                               \
   do {                                                                                                     \
      VkResult r_ = (x);                                                                                    \
      if (r_ != VK_SUCCESS) {                                                                               \
         printf("RESULT %s FAIL %s = %d\n", rung, #x, r_);                                                 \
         finish(1);                                                                                         \
      }                                                                                                     \
   } while (0)

static double now_ms(void)
{
   struct timespec t;
   clock_gettime(CLOCK_MONOTONIC, &t);
   return t.tv_sec * 1e3 + t.tv_nsec / 1e6;
}

/* ---- objects ---- */
static uint32_t memtype(uint32_t bits, VkMemoryPropertyFlags want)
{
   for (uint32_t i = 0; i < memprops.memoryTypeCount; i++)
      if ((bits & (1u << i)) && (memprops.memoryTypes[i].propertyFlags & want) == want)
         return i;
   return 0;
}

struct buf {
   VkBuffer b;
   VkDeviceMemory m;
   void *p;
};

static struct buf newbuf(VkDeviceSize size, VkBufferUsageFlags usage)
{
   struct buf r;
   const VkBufferCreateInfo bi = { .sType = VK_STRUCTURE_TYPE_BUFFER_CREATE_INFO, .size = size, .usage = usage };
   CK(vkCreateBuffer(dev, &bi, NULL, &r.b));
   VkMemoryRequirements mr;
   vkGetBufferMemoryRequirements(dev, r.b, &mr);
   const VkMemoryAllocateInfo ai = {
      .sType = VK_STRUCTURE_TYPE_MEMORY_ALLOCATE_INFO, .allocationSize = mr.size,
      .memoryTypeIndex = memtype(mr.memoryTypeBits,
                                 VK_MEMORY_PROPERTY_HOST_VISIBLE_BIT | VK_MEMORY_PROPERTY_HOST_COHERENT_BIT) };
   CK(vkAllocateMemory(dev, &ai, NULL, &r.m));
   CK(vkBindBufferMemory(dev, r.b, r.m, 0));
   CK(vkMapMemory(dev, r.m, 0, VK_WHOLE_SIZE, 0, &r.p));
   memset(r.p, 0, size);
   return r;
}

struct img {
   VkImage i;
   VkDeviceMemory m;
   VkImageView v;
   uint32_t w, h;
   VkFormat f;
};

/* host: a linear-tiling image in host-visible memory, created PREINITIALIZED (texhostlinear) */
static struct img newimg_ex(uint32_t w, uint32_t h, VkFormat f, VkImageUsageFlags usage, uint32_t mips,
                            uint32_t view_levels, int host);
static struct img newimg(uint32_t w, uint32_t h, VkFormat f, VkImageUsageFlags usage, uint32_t mips)
{
   return newimg_ex(w, h, f, usage, mips, 1, 0);
}

static struct img newimg_ex(uint32_t w, uint32_t h, VkFormat f, VkImageUsageFlags usage, uint32_t mips,
                            uint32_t view_levels, int host)
{
   struct img r = { .w = w, .h = h, .f = f };
   const int depth = f == VK_FORMAT_D32_SFLOAT || f == VK_FORMAT_D16_UNORM;
   const VkImageCreateInfo ii = { .sType = VK_STRUCTURE_TYPE_IMAGE_CREATE_INFO, .imageType = VK_IMAGE_TYPE_2D,
                                  .format = f, .extent = { w, h, 1 }, .mipLevels = mips, .arrayLayers = 1,
                                  .samples = VK_SAMPLE_COUNT_1_BIT,
                                  .tiling = host ? VK_IMAGE_TILING_LINEAR : VK_IMAGE_TILING_OPTIMAL,
                                  .usage = usage,
                                  .initialLayout = host ? VK_IMAGE_LAYOUT_PREINITIALIZED : VK_IMAGE_LAYOUT_UNDEFINED };
   CK(vkCreateImage(dev, &ii, NULL, &r.i));
   VkMemoryRequirements mr;
   vkGetImageMemoryRequirements(dev, r.i, &mr);
   const VkMemoryAllocateInfo ai = { .sType = VK_STRUCTURE_TYPE_MEMORY_ALLOCATE_INFO, .allocationSize = mr.size,
                                     .memoryTypeIndex = memtype(mr.memoryTypeBits, host ? VK_MEMORY_PROPERTY_HOST_VISIBLE_BIT |
                                                                VK_MEMORY_PROPERTY_HOST_COHERENT_BIT : 0) };
   CK(vkAllocateMemory(dev, &ai, NULL, &r.m));
   CK(vkBindImageMemory(dev, r.i, r.m, 0));
   const VkImageViewCreateInfo vi = {
      .sType = VK_STRUCTURE_TYPE_IMAGE_VIEW_CREATE_INFO, .image = r.i, .viewType = VK_IMAGE_VIEW_TYPE_2D,
      .format = f,
      .subresourceRange = { depth ? VK_IMAGE_ASPECT_DEPTH_BIT : VK_IMAGE_ASPECT_COLOR_BIT, 0, view_levels, 0, 1 } };
   CK(vkCreateImageView(dev, &vi, NULL, &r.v));
   return r;
}

/* a target: colour attachment, copied out, optionally sampled */
static struct img newtarget(uint32_t w, uint32_t h, VkImageUsageFlags extra)
{
   return newimg(w, h, VK_FORMAT_B8G8R8A8_UNORM,
                 VK_IMAGE_USAGE_COLOR_ATTACHMENT_BIT | VK_IMAGE_USAGE_TRANSFER_SRC_BIT | extra, 2);
}

static VkRenderPass newrp_ex(VkFormat depth, VkAttachmentLoadOp load, VkImageLayout initial, VkImageLayout final);
static VkRenderPass newrp(VkFormat depth, VkImageLayout final)
{
   return newrp_ex(depth, VK_ATTACHMENT_LOAD_OP_CLEAR, VK_IMAGE_LAYOUT_UNDEFINED, final);
}

static VkRenderPass newrp_ex(VkFormat depth, VkAttachmentLoadOp load, VkImageLayout initial, VkImageLayout final)
{
   const VkAttachmentDescription ad[2] = {
      { .format = VK_FORMAT_B8G8R8A8_UNORM, .samples = VK_SAMPLE_COUNT_1_BIT, .loadOp = load,
        .storeOp = VK_ATTACHMENT_STORE_OP_STORE, .stencilLoadOp = VK_ATTACHMENT_LOAD_OP_DONT_CARE,
        .stencilStoreOp = VK_ATTACHMENT_STORE_OP_DONT_CARE, .initialLayout = initial,
        .finalLayout = final },
      { .format = depth, .samples = VK_SAMPLE_COUNT_1_BIT, .loadOp = VK_ATTACHMENT_LOAD_OP_CLEAR,
        .storeOp = VK_ATTACHMENT_STORE_OP_DONT_CARE, .stencilLoadOp = VK_ATTACHMENT_LOAD_OP_DONT_CARE,
        .stencilStoreOp = VK_ATTACHMENT_STORE_OP_DONT_CARE, .initialLayout = VK_IMAGE_LAYOUT_UNDEFINED,
        .finalLayout = VK_IMAGE_LAYOUT_DEPTH_STENCIL_ATTACHMENT_OPTIMAL },
   };
   const VkAttachmentReference cr = { 0, VK_IMAGE_LAYOUT_COLOR_ATTACHMENT_OPTIMAL };
   const VkAttachmentReference dr = { 1, VK_IMAGE_LAYOUT_DEPTH_STENCIL_ATTACHMENT_OPTIMAL };
   const VkSubpassDescription sd = { .pipelineBindPoint = VK_PIPELINE_BIND_POINT_GRAPHICS,
                                     .colorAttachmentCount = 1, .pColorAttachments = &cr,
                                     .pDepthStencilAttachment = depth ? &dr : NULL };
   const VkRenderPassCreateInfo rpi = { .sType = VK_STRUCTURE_TYPE_RENDER_PASS_CREATE_INFO,
                                        .attachmentCount = depth ? 2 : 1, .pAttachments = ad, .subpassCount = 1,
                                        .pSubpasses = &sd };
   VkRenderPass rp;
   CK(vkCreateRenderPass(dev, &rpi, NULL, &rp));
   return rp;
}

static VkFramebuffer newfb(VkRenderPass rp, const struct img *c, const struct img *d)
{
   const VkImageView v[2] = { c->v, d ? d->v : VK_NULL_HANDLE };
   const VkFramebufferCreateInfo fi = { .sType = VK_STRUCTURE_TYPE_FRAMEBUFFER_CREATE_INFO, .renderPass = rp,
                                        .attachmentCount = d ? 2 : 1, .pAttachments = v, .width = c->w,
                                        .height = c->h, .layers = 1 };
   VkFramebuffer fb;
   CK(vkCreateFramebuffer(dev, &fi, NULL, &fb));
   return fb;
}

static VkShaderModule shader(const uint32_t *code, size_t size)
{
   const VkShaderModuleCreateInfo si = { .sType = VK_STRUCTURE_TYPE_SHADER_MODULE_CREATE_INFO, .codeSize = size,
                                         .pCode = code };
   VkShaderModule m;
   CK(vkCreateShaderModule(dev, &si, NULL, &m));
   return m;
}
#define SHADER(x) shader(x, sizeof(x))

/* push constants: rect (NDC x0 y0 x1 y1), colour, z */
struct pc {
   float rect[4], color[4], z[4];
};

static VkPipelineLayout newlayout(VkDescriptorSetLayout dsl)
{
   const VkPushConstantRange pr = { VK_SHADER_STAGE_VERTEX_BIT | VK_SHADER_STAGE_FRAGMENT_BIT, 0,
                                    sizeof(struct pc) };
   const VkPipelineLayoutCreateInfo li = { .sType = VK_STRUCTURE_TYPE_PIPELINE_LAYOUT_CREATE_INFO,
                                           .setLayoutCount = dsl ? 1 : 0, .pSetLayouts = &dsl,
                                           .pushConstantRangeCount = 1, .pPushConstantRanges = &pr };
   VkPipelineLayout l;
   CK(vkCreatePipelineLayout(dev, &li, NULL, &l));
   return l;
}

static VkDescriptorSetLayout newdsl(VkDescriptorType type, VkShaderStageFlags stages)
{
   const VkDescriptorSetLayoutBinding b = { 0, type, 1, stages };
   const VkDescriptorSetLayoutCreateInfo li = { .sType = VK_STRUCTURE_TYPE_DESCRIPTOR_SET_LAYOUT_CREATE_INFO,
                                                .bindingCount = 1, .pBindings = &b };
   VkDescriptorSetLayout l;
   CK(vkCreateDescriptorSetLayout(dev, &li, NULL, &l));
   return l;
}

static VkDescriptorSet newset(VkDescriptorSetLayout dsl, VkDescriptorType type, const VkDescriptorBufferInfo *bi,
                              const VkDescriptorImageInfo *ii)
{
   const VkDescriptorPoolSize ps = { type, 1 };
   const VkDescriptorPoolCreateInfo pi = { .sType = VK_STRUCTURE_TYPE_DESCRIPTOR_POOL_CREATE_INFO, .maxSets = 1,
                                           .poolSizeCount = 1, .pPoolSizes = &ps };
   VkDescriptorPool dp;
   CK(vkCreateDescriptorPool(dev, &pi, NULL, &dp));
   const VkDescriptorSetAllocateInfo ai = { .sType = VK_STRUCTURE_TYPE_DESCRIPTOR_SET_ALLOCATE_INFO,
                                            .descriptorPool = dp, .descriptorSetCount = 1, .pSetLayouts = &dsl };
   VkDescriptorSet s;
   CK(vkAllocateDescriptorSets(dev, &ai, &s));
   const VkWriteDescriptorSet w = { .sType = VK_STRUCTURE_TYPE_WRITE_DESCRIPTOR_SET, .dstSet = s,
                                    .descriptorCount = 1, .descriptorType = type, .pBufferInfo = bi,
                                    .pImageInfo = ii };
   vkUpdateDescriptorSets(dev, 1, &w, 0, NULL);
   return s;
}

enum vin { VIN_POS4, VIN_CORNER2, VIN_POS2_COL4, VIN_POS3_NRM3, VIN_POS2_TC2, VIN_POS3_NRM3_TC2 };

struct pipe_cfg {
   VkShaderModule vs, fs;
   enum vin vin;
   int blend, depth;
   VkPipelineLayout layout;
   VkRenderPass rp;
   uint32_t w, h;
};

static VkPipeline newpipe(const struct pipe_cfg *c)
{
   const VkPipelineShaderStageCreateInfo st[2] = {
      { .sType = VK_STRUCTURE_TYPE_PIPELINE_SHADER_STAGE_CREATE_INFO, .stage = VK_SHADER_STAGE_VERTEX_BIT,
        .module = c->vs, .pName = "main" },
      { .sType = VK_STRUCTURE_TYPE_PIPELINE_SHADER_STAGE_CREATE_INFO, .stage = VK_SHADER_STAGE_FRAGMENT_BIT,
        .module = c->fs, .pName = "main" },
   };
   VkVertexInputBindingDescription vb = { 0, 16, VK_VERTEX_INPUT_RATE_VERTEX };
   VkVertexInputAttributeDescription va[3] = { { 0, 0, VK_FORMAT_R32G32B32A32_SFLOAT, 0 } };
   uint32_t nva = 1;
   if (c->vin == VIN_CORNER2) {
      vb.stride = 8;
      va[0].format = VK_FORMAT_R32G32_SFLOAT;
   } else if (c->vin == VIN_POS3_NRM3) {
      vb.stride = 24;
      va[0].format = VK_FORMAT_R32G32B32_SFLOAT;
      va[1] = (VkVertexInputAttributeDescription){ 1, 0, VK_FORMAT_R32G32B32_SFLOAT, 12 };
      nva = 2;
   } else if (c->vin == VIN_POS2_TC2) {
      va[0].format = VK_FORMAT_R32G32_SFLOAT;
      va[1] = (VkVertexInputAttributeDescription){ 1, 0, VK_FORMAT_R32G32_SFLOAT, 8 };
      nva = 2;
   } else if (c->vin == VIN_POS3_NRM3_TC2) {
      vb.stride = 32;
      va[0].format = VK_FORMAT_R32G32B32_SFLOAT;
      va[1] = (VkVertexInputAttributeDescription){ 1, 0, VK_FORMAT_R32G32B32_SFLOAT, 12 };
      va[2] = (VkVertexInputAttributeDescription){ 2, 0, VK_FORMAT_R32G32_SFLOAT, 24 };
      nva = 3;
   } else if (c->vin == VIN_POS2_COL4) {
      vb.stride = 24;
      va[0].format = VK_FORMAT_R32G32_SFLOAT;
      va[1] = (VkVertexInputAttributeDescription){ 1, 0, VK_FORMAT_R32G32B32A32_SFLOAT, 8 };
      nva = 2;
   }
   const VkPipelineVertexInputStateCreateInfo vis = {
      .sType = VK_STRUCTURE_TYPE_PIPELINE_VERTEX_INPUT_STATE_CREATE_INFO, .vertexBindingDescriptionCount = 1,
      .pVertexBindingDescriptions = &vb, .vertexAttributeDescriptionCount = nva, .pVertexAttributeDescriptions = va };
   const VkPipelineInputAssemblyStateCreateInfo ias = {
      .sType = VK_STRUCTURE_TYPE_PIPELINE_INPUT_ASSEMBLY_STATE_CREATE_INFO,
      .topology = VK_PRIMITIVE_TOPOLOGY_TRIANGLE_LIST };
   const VkViewport vp = { 0, 0, (float)c->w, (float)c->h, 0, 1 };
   const VkRect2D sc = { { 0, 0 }, { c->w, c->h } };
   const VkPipelineViewportStateCreateInfo vps = { .sType = VK_STRUCTURE_TYPE_PIPELINE_VIEWPORT_STATE_CREATE_INFO,
                                                   .viewportCount = 1, .pViewports = &vp, .scissorCount = 1,
                                                   .pScissors = &sc };
   const VkPipelineRasterizationStateCreateInfo rs = {
      .sType = VK_STRUCTURE_TYPE_PIPELINE_RASTERIZATION_STATE_CREATE_INFO, .polygonMode = VK_POLYGON_MODE_FILL,
      .cullMode = VK_CULL_MODE_NONE, .frontFace = VK_FRONT_FACE_COUNTER_CLOCKWISE, .lineWidth = 1.0f };
   const VkPipelineMultisampleStateCreateInfo ms = { .sType = VK_STRUCTURE_TYPE_PIPELINE_MULTISAMPLE_STATE_CREATE_INFO,
                                                     .rasterizationSamples = VK_SAMPLE_COUNT_1_BIT };
   const VkPipelineDepthStencilStateCreateInfo ds = {
      .sType = VK_STRUCTURE_TYPE_PIPELINE_DEPTH_STENCIL_STATE_CREATE_INFO, .depthTestEnable = VK_TRUE,
      .depthWriteEnable = VK_TRUE, .depthCompareOp = VK_COMPARE_OP_LESS };
   const VkPipelineColorBlendAttachmentState cba = {
      .blendEnable = c->blend ? VK_TRUE : VK_FALSE, .srcColorBlendFactor = VK_BLEND_FACTOR_SRC_ALPHA,
      .dstColorBlendFactor = VK_BLEND_FACTOR_ONE_MINUS_SRC_ALPHA, .colorBlendOp = VK_BLEND_OP_ADD,
      .srcAlphaBlendFactor = VK_BLEND_FACTOR_ONE, .dstAlphaBlendFactor = VK_BLEND_FACTOR_ONE_MINUS_SRC_ALPHA,
      .alphaBlendOp = VK_BLEND_OP_ADD, .colorWriteMask = 0xf };
   const VkPipelineColorBlendStateCreateInfo cbs = { .sType = VK_STRUCTURE_TYPE_PIPELINE_COLOR_BLEND_STATE_CREATE_INFO,
                                                     .attachmentCount = 1, .pAttachments = &cba };
   const VkGraphicsPipelineCreateInfo gi = {
      .sType = VK_STRUCTURE_TYPE_GRAPHICS_PIPELINE_CREATE_INFO, .stageCount = 2, .pStages = st,
      .pVertexInputState = &vis, .pInputAssemblyState = &ias, .pViewportState = &vps, .pRasterizationState = &rs,
      .pMultisampleState = &ms, .pDepthStencilState = c->depth ? &ds : NULL, .pColorBlendState = &cbs,
      .layout = c->layout, .renderPass = c->rp };
   VkPipeline p;
   step("vkCreateGraphicsPipelines");
   const VkResult pr = vkCreateGraphicsPipelines(dev, VK_NULL_HANDLE, 1, &gi, NULL, &p);
   if (pr != VK_SUCCESS) {
      printf("RESULT %s %s refused at pipeline creation (vkCreateGraphicsPipelines = %d; the reason is the G5P line "
             "above)\n", rung, skip_refusal ? "SKIP" : "FAIL", pr);
      finish(skip_refusal ? 0 : 1);
   }
   return p;
}

static VkCommandBuffer begin(int reusable)
{
   const VkCommandBufferAllocateInfo ai = { .sType = VK_STRUCTURE_TYPE_COMMAND_BUFFER_ALLOCATE_INFO,
                                            .commandPool = pool, .level = VK_COMMAND_BUFFER_LEVEL_PRIMARY,
                                            .commandBufferCount = 1 };
   VkCommandBuffer cmd;
   CK(vkAllocateCommandBuffers(dev, &ai, &cmd));
   const VkCommandBufferBeginInfo bi = { .sType = VK_STRUCTURE_TYPE_COMMAND_BUFFER_BEGIN_INFO,
                                         .flags = reusable ? 0 : VK_COMMAND_BUFFER_USAGE_ONE_TIME_SUBMIT_BIT };
   CK(vkBeginCommandBuffer(cmd, &bi));
   return cmd;
}

/* submit and wait at most timeout_ns; TIMEOUT ends the process */
static void submit(VkCommandBuffer cmd)
{
   static VkFence fence;
   if (!fence) {
      const VkFenceCreateInfo fi = { .sType = VK_STRUCTURE_TYPE_FENCE_CREATE_INFO };
      CK(vkCreateFence(dev, &fi, NULL, &fence));
   } else {
      CK(vkResetFences(dev, 1, &fence));
   }
   const VkSubmitInfo si = { .sType = VK_STRUCTURE_TYPE_SUBMIT_INFO, .commandBufferCount = 1,
                             .pCommandBuffers = &cmd };
   step("submit %u", njobs + 1);
   const double t0 = now_ms();
   CK(vkQueueSubmit(q, 1, &si, fence));
   step("wait %u", njobs + 1);
   const VkResult r = vkWaitForFences(dev, 1, &fence, VK_TRUE, timeout_ns);
   last_ms = now_ms() - t0;
   if (r == VK_TIMEOUT) {
      timed_out = 1;
      printf("RESULT %s TIMEOUT fence not signalled after %.0f ms (job %u)\n", rung, last_ms, njobs + 1);
      finish(3);
   }
   step("done %u %.2f ms", njobs + 1, last_ms);
   if (r != VK_SUCCESS) {
      printf("RESULT %s FAIL vkWaitForFences = %d after %.1f ms (job %u)\n", rung, r, last_ms, njobs + 1);
      finish(1);
   }
   njobs++;
   sum_ms += last_ms;
   if (last_ms > max_ms)
      max_ms = last_ms;
}

/* a failing vkEndCommandBuffer is a G5P refusal (its reason is the "G5P refuses:" line on stderr) */
static void end_submit(VkCommandBuffer cmd)
{
   const VkResult r = vkEndCommandBuffer(cmd);
   if (r != VK_SUCCESS) {
      if (skip_refusal) {
         printf("RESULT %s SKIP refused by the A8 driver (vkEndCommandBuffer = %d)\n", rung, r);
         finish(0);
      }
      printf("RESULT %s FAIL refused by Mesa (vkEndCommandBuffer = %d; the reason is the G5P line above)\n", rung, r);
      finish(1);
   }
   submit(cmd);
}

static void barrier(VkCommandBuffer cmd, VkPipelineStageFlags src, VkAccessFlags sa, VkPipelineStageFlags dst,
                    VkAccessFlags da)
{
   const VkMemoryBarrier mb = { .sType = VK_STRUCTURE_TYPE_MEMORY_BARRIER, .srcAccessMask = sa, .dstAccessMask = da };
   vkCmdPipelineBarrier(cmd, src, dst, 0, 1, &mb, 0, NULL, 0, NULL);
}

static void image_barrier(VkCommandBuffer cmd, VkImage img, VkImageLayout from, VkImageLayout to,
                          VkPipelineStageFlags src, VkAccessFlags sa, VkPipelineStageFlags dst, VkAccessFlags da)
{
   const VkImageMemoryBarrier b = { .sType = VK_STRUCTURE_TYPE_IMAGE_MEMORY_BARRIER, .srcAccessMask = sa,
                                    .dstAccessMask = da, .oldLayout = from, .newLayout = to,
                                    .srcQueueFamilyIndex = VK_QUEUE_FAMILY_IGNORED,
                                    .dstQueueFamilyIndex = VK_QUEUE_FAMILY_IGNORED, .image = img,
                                    .subresourceRange = { VK_IMAGE_ASPECT_COLOR_BIT, 0, VK_REMAINING_MIP_LEVELS, 0, 1 } };
   vkCmdPipelineBarrier(cmd, src, dst, 0, 0, NULL, 0, NULL, 1, &b);
}

static void begin_pass(VkCommandBuffer cmd, VkRenderPass rp, VkFramebuffer fb, uint32_t w, uint32_t h,
                       const float clear[4], int depth)
{
   VkClearValue cv[2];
   memcpy(cv[0].color.float32, clear, 16);
   cv[1].depthStencil = (VkClearDepthStencilValue){ 1.0f, 0 };
   const VkRenderPassBeginInfo bi = { .sType = VK_STRUCTURE_TYPE_RENDER_PASS_BEGIN_INFO, .renderPass = rp,
                                      .framebuffer = fb, .renderArea = { { 0, 0 }, { w, h } },
                                      .clearValueCount = depth ? 2 : 1, .pClearValues = cv };
   vkCmdBeginRenderPass(cmd, &bi, VK_SUBPASS_CONTENTS_INLINE);
}

/* the target's level 0 into a host buffer (the target is in TRANSFER_SRC_OPTIMAL after its pass) */
static void readback(VkCommandBuffer cmd, const struct img *t, const struct buf *rb)
{
   const VkBufferImageCopy c = { .imageSubresource = { VK_IMAGE_ASPECT_COLOR_BIT, 0, 0, 1 },
                                 .imageExtent = { t->w, t->h, 1 } };
   vkCmdCopyImageToBuffer(cmd, t->i, VK_IMAGE_LAYOUT_TRANSFER_SRC_OPTIMAL, rb->b, 1, &c);
   barrier(cmd, VK_PIPELINE_STAGE_TRANSFER_BIT, VK_ACCESS_TRANSFER_WRITE_BIT, VK_PIPELINE_STAGE_HOST_BIT,
           VK_ACCESS_HOST_READ_BIT);
}

/* ---- expected images ---- */
static uint8_t u8(float f)
{
   const long v = lroundf(f * 255.0f);
   return v < 0 ? 0 : v > 255 ? 255 : (uint8_t)v;
}

static void bgra(uint8_t *p, const float c[4])
{
   p[0] = u8(c[2]);
   p[1] = u8(c[1]);
   p[2] = u8(c[0]);
   p[3] = u8(c[3]);
}

struct expect {
   uint32_t w, h;
   uint8_t *px;   /* BGRA8 */
   uint8_t *skip; /* 1 = not judged */
};

static struct expect newexpect(uint32_t w, uint32_t h, const float clear[4])
{
   struct expect e = { w, h, malloc((size_t)w * h * 4), calloc((size_t)w * h, 1) };
   for (size_t i = 0; i < (size_t)w * h; i++)
      bgra(e.px + 4 * i, clear);
   return e;
}

/* NDC rectangle, as the rasterizer covers it: pixel centres inside [x0, x1) x [y0, y1) */
static void expect_rect(struct expect *e, const float r[4], const float c[4])
{
   for (uint32_t y = 0; y < e->h; y++)
      for (uint32_t x = 0; x < e->w; x++) {
         const float nx = (x + 0.5f) / e->w * 2 - 1, ny = (y + 0.5f) / e->h * 2 - 1;
         if (nx > fminf(r[0], r[2]) && nx < fmaxf(r[0], r[2]) && ny > fminf(r[1], r[3]) && ny < fmaxf(r[1], r[3]))
            bgra(e->px + 4 * ((size_t)y * e->w + x), c);
      }
}

/* the smoke triangle (-0.5,-0.5) (0.5,-0.5) (0,0.5), edges +-0.75 px not judged */
static void expect_tri(struct expect *e, const float c[4])
{
   const float vx[3] = { 0.25f * e->w, 0.75f * e->w, 0.5f * e->w }, vy[3] = { 0.25f * e->h, 0.25f * e->h, 0.75f * e->h };
   for (uint32_t y = 0; y < e->h; y++)
      for (uint32_t x = 0; x < e->w; x++) {
         const float px = x + 0.5f, py = y + 0.5f;
         float d = 1e9f;
         for (int i = 0; i < 3; i++) {
            const int j = (i + 1) % 3;
            const float ex = vx[j] - vx[i], ey = vy[j] - vy[i];
            d = fminf(d, (ex * (py - vy[i]) - ey * (px - vx[i])) / sqrtf(ex * ex + ey * ey));
         }
         const size_t k = (size_t)y * e->w + x;
         if (fabsf(d) <= 0.75f)
            e->skip[k] = 1;
         else if (d > 0)
            bgra(e->px + 4 * k, c);
      }
}

/* compare; prints the RESULT line (with extra) and ends the process on a failure */
static void check(const struct expect *e, const uint8_t *got, int tol, const char *extra)
{
   if (nocheck) {
      printf("RESULT %s PASS (not checked) %ux%u %s\n", rung, e->w, e->h, extra ? extra : "");
      finish(0);
   }
   size_t bad = 0, judged = 0, first = (size_t)-1;
   for (size_t i = 0; i < (size_t)e->w * e->h; i++) {
      if (e->skip[i])
         continue;
      judged++;
      for (int c = 0; c < 4; c++)
         if (abs((int)got[4 * i + c] - (int)e->px[4 * i + c]) > tol) {
            if (first == (size_t)-1)
               first = i;
            bad++;
            break;
         }
   }
   if (!bad) {
      printf("RESULT %s PASS %ux%u %zu/%zu pixels %s\n", rung, e->w, e->h, judged, judged, extra ? extra : "");
      finish(0);
   }
   const uint8_t *g = got + 4 * first, *x = e->px + 4 * first;
   printf("RESULT %s FAIL %ux%u %zu/%zu pixels wrong, first (%zu,%zu) got %02x%02x%02x%02x want %02x%02x%02x%02x %s\n",
          rung, e->w, e->h, bad, judged, first % e->w, first / e->w, g[0], g[1], g[2], g[3], x[0], x[1], x[2], x[3],
          extra ? extra : "");
   finish(1);
}

/* ---- common draws ---- */
static const float CLEAR[4] = { 0.0f, 0.0f, 0.25f, 1.0f };
static const float TRI[4] = { 1.0f, 0.25f, 0.0f, 1.0f };
static const float TRI_V[12] = { -0.5f, -0.5f, 0, 1, 0.5f, -0.5f, 0, 1, 0, 0.5f, 0, 1 };
static const float CORNERS[12] = { 0, 0, 1, 0, 0, 1, 1, 0, 1, 1, 0, 1 };

static void rect_draw_uv(VkCommandBuffer cmd, VkPipelineLayout l, const float r[4], const float c[4], float z,
                         float su, float sv)
{
   struct pc pc = { { r[0], r[1], r[2], r[3] }, { c[0], c[1], c[2], c[3] }, { z, su, sv, 0 } };
   vkCmdPushConstants(cmd, l, VK_SHADER_STAGE_VERTEX_BIT | VK_SHADER_STAGE_FRAGMENT_BIT, 0, sizeof(pc), &pc);
   vkCmdDraw(cmd, 6, 1, 0, 0);
}

static void rect_draw(VkCommandBuffer cmd, VkPipelineLayout l, const float r[4], const float c[4], float z)
{
   struct pc pc = { { r[0], r[1], r[2], r[3] }, { c[0], c[1], c[2], c[3] }, { z, 1, 1, 0 } };
   vkCmdPushConstants(cmd, l, VK_SHADER_STAGE_VERTEX_BIT | VK_SHADER_STAGE_FRAGMENT_BIT, 0, sizeof(pc), &pc);
   vkCmdDraw(cmd, 6, 1, 0, 0);
}

/* the 4x4 grid of rung draws16: cell i in colour i */
static void grid_colour(int i, float c[4])
{
   static const float lv[4] = { 0.0f, 0.25f, 0.75f, 1.0f };
   c[0] = lv[i & 3];
   c[1] = lv[(i >> 2) & 3];
   c[2] = lv[(i + 1) & 3];
   c[3] = 1.0f;
}

static void grid_rect(int i, float r[4])
{
   const int cx = i & 3, cy = i >> 2;
   r[0] = -1 + cx * 0.5f;
   r[1] = -1 + cy * 0.5f;
   r[2] = r[0] + 0.5f;
   r[3] = r[1] + 0.5f;
}

/* ---- rungs ---- */
static void r_clear(uint32_t w, uint32_t h)
{
   struct img t = newtarget(w, h, 0);
   VkRenderPass rp = newrp(VK_FORMAT_UNDEFINED, VK_IMAGE_LAYOUT_TRANSFER_SRC_OPTIMAL);
   VkFramebuffer fb = newfb(rp, &t, NULL);
   struct buf rb = newbuf((VkDeviceSize)w * h * 4, VK_BUFFER_USAGE_TRANSFER_DST_BIT);
   const float clear[4] = { 0.25f, 0.5f, 0.75f, 1.0f };
   VkCommandBuffer cmd = begin(0);
   begin_pass(cmd, rp, fb, w, h, clear, 0);
   vkCmdEndRenderPass(cmd);
   readback(cmd, &t, &rb);
   end_submit(cmd);
   struct expect e = newexpect(w, h, clear);
   char d[64];
   snprintf(d, sizeof(d), "job %.1f ms", last_ms);
   check(&e, rb.p, 1, d);
}

/* the triangle pipeline and its vertex buffer */
struct tri {
   struct img t;
   VkRenderPass rp;
   VkFramebuffer fb;
   VkPipeline p;
   struct buf vb, rb;
};

static struct tri tri_setup(uint32_t w, uint32_t h)
{
   struct tri s;
   s.t = newtarget(w, h, 0);
   s.rp = newrp(VK_FORMAT_UNDEFINED, VK_IMAGE_LAYOUT_TRANSFER_SRC_OPTIMAL);
   s.fb = newfb(s.rp, &s.t, NULL);
   const struct pipe_cfg pc = { SHADER(l_tri_vert), SHADER(l_const_frag), VIN_POS4, 0, 0, newlayout(0), s.rp, w, h };
   s.p = newpipe(&pc);
   s.vb = newbuf(sizeof(TRI_V), VK_BUFFER_USAGE_VERTEX_BUFFER_BIT);
   memcpy(s.vb.p, TRI_V, sizeof(TRI_V));
   s.rb = newbuf((VkDeviceSize)w * h * 4, VK_BUFFER_USAGE_TRANSFER_DST_BIT);
   return s;
}

static void tri_record(VkCommandBuffer cmd, struct tri *s, VkBuffer vb)
{
   begin_pass(cmd, s->rp, s->fb, s->t.w, s->t.h, CLEAR, 0);
   vkCmdBindPipeline(cmd, VK_PIPELINE_BIND_POINT_GRAPHICS, s->p);
   const VkDeviceSize off = 0;
   vkCmdBindVertexBuffers(cmd, 0, 1, &vb, &off);
   vkCmdDraw(cmd, 3, 1, 0, 0);
   vkCmdEndRenderPass(cmd);
   readback(cmd, &s->t, &s->rb);
}

static void r_tri(uint32_t w, uint32_t h)
{
   struct tri s = tri_setup(w, h);
   VkCommandBuffer cmd = begin(0);
   tri_record(cmd, &s, s.vb.b);
   end_submit(cmd);
   struct expect e = newexpect(w, h, CLEAR);
   expect_tri(&e, TRI);
   char d[64];
   snprintf(d, sizeof(d), "job %.1f ms", last_ms);
   check(&e, s.rb.p, 1, d);
}

static void r_frames(void)
{
   struct tri s = tri_setup(64, 64);
   VkCommandBuffer cmd = begin(1);
   tri_record(cmd, &s, s.vb.b);
   CK(vkEndCommandBuffer(cmd));
   struct expect e = newexpect(64, 64, CLEAR);
   expect_tri(&e, TRI);
   for (int f = 0; f < 100; f++) {
      memset(s.rb.p, 0x5a, 64 * 64 * 4);
      submit(cmd);
      if (!nocheck && f < 99) {
         size_t bad = 0;
         for (size_t i = 0; i < 64 * 64; i++)
            if (!e.skip[i] && memcmp((uint8_t *)s.rb.p + 4 * i, e.px + 4 * i, 4) &&
                (abs(((uint8_t *)s.rb.p)[4 * i] - e.px[4 * i]) > 1 ||
                 abs(((uint8_t *)s.rb.p)[4 * i + 1] - e.px[4 * i + 1]) > 1 ||
                 abs(((uint8_t *)s.rb.p)[4 * i + 2] - e.px[4 * i + 2]) > 1 ||
                 abs(((uint8_t *)s.rb.p)[4 * i + 3] - e.px[4 * i + 3]) > 1))
               bad++;
         if (bad) {
            printf("RESULT %s FAIL frame %d: %zu pixels wrong (avg %.2f ms, max %.2f ms over %u jobs)\n", rung, f,
                   bad, sum_ms / njobs, max_ms, njobs);
            finish(1);
         }
      }
   }
   char d[96];
   snprintf(d, sizeof(d), "100 frames, fence wait avg %.2f ms max %.2f ms", sum_ms / njobs, max_ms);
   check(&e, s.rb.p, 1, d);
}

/* the rectangle pipeline (push constants), optional blend / depth / descriptor set */
struct rect {
   struct img t, d;
   VkRenderPass rp;
   VkFramebuffer fb;
   VkPipelineLayout l;
   VkPipeline p;
   struct buf vb, rb;
};

static struct rect rect_setup(uint32_t w, uint32_t h, VkShaderModule fs, int blend, VkFormat depth,
                              VkDescriptorSetLayout dsl, VkImageUsageFlags extra, VkImageLayout final)
{
   struct rect s = { 0 };
   s.t = newtarget(w, h, extra);
   if (depth)
      s.d = newimg(w, h, depth, VK_IMAGE_USAGE_DEPTH_STENCIL_ATTACHMENT_BIT, 1);
   s.rp = newrp(depth, final);
   s.fb = newfb(s.rp, &s.t, depth ? &s.d : NULL);
   s.l = newlayout(dsl);
   const struct pipe_cfg pc = { SHADER(l_rect_vert), fs, VIN_CORNER2, blend, depth != 0, s.l, s.rp, w, h };
   s.p = newpipe(&pc);
   s.vb = newbuf(sizeof(CORNERS), VK_BUFFER_USAGE_VERTEX_BUFFER_BIT);
   memcpy(s.vb.p, CORNERS, sizeof(CORNERS));
   s.rb = newbuf((VkDeviceSize)w * h * 4, VK_BUFFER_USAGE_TRANSFER_DST_BIT);
   return s;
}

static void rect_begin(VkCommandBuffer cmd, struct rect *s, const float clear[4])
{
   begin_pass(cmd, s->rp, s->fb, s->t.w, s->t.h, clear, s->d.i != VK_NULL_HANDLE);
   vkCmdBindPipeline(cmd, VK_PIPELINE_BIND_POINT_GRAPHICS, s->p);
   const VkDeviceSize off = 0;
   vkCmdBindVertexBuffers(cmd, 0, 1, &s->vb.b, &off);
}

static const float FULL[4] = { -1, -1, 1, 1 };

static void r_pc(void)
{
   struct rect s = rect_setup(64, 64, SHADER(l_pc_frag), 0, 0, 0, 0, VK_IMAGE_LAYOUT_TRANSFER_SRC_OPTIMAL);
   const float c[4] = { 0.75f, 0.25f, 1.0f, 1.0f }, r[4] = { -0.5f, -1.0f, 0.75f, 0.5f };
   VkCommandBuffer cmd = begin(0);
   rect_begin(cmd, &s, CLEAR);
   rect_draw(cmd, s.l, r, c, 0.5f);
   vkCmdEndRenderPass(cmd);
   readback(cmd, &s.t, &s.rb);
   end_submit(cmd);
   struct expect e = newexpect(64, 64, CLEAR);
   expect_rect(&e, r, c);
   check(&e, s.rb.p, 1, "one push-constant rectangle");
}

static void grid_draws(VkCommandBuffer cmd, VkPipelineLayout l)
{
   for (int i = 0; i < 16; i++) {
      float r[4], c[4];
      grid_rect(i, r);
      grid_colour(i, c);
      rect_draw(cmd, l, r, c, 0.5f);
   }
}

static void grid_expect(struct expect *e)
{
   for (int i = 0; i < 16; i++) {
      float r[4], c[4];
      grid_rect(i, r);
      grid_colour(i, c);
      expect_rect(e, r, c);
   }
}

static void r_draws16(void)
{
   struct rect s = rect_setup(64, 64, SHADER(l_pc_frag), 0, 0, 0, 0, VK_IMAGE_LAYOUT_TRANSFER_SRC_OPTIMAL);
   VkCommandBuffer cmd = begin(0);
   rect_begin(cmd, &s, CLEAR);
   grid_draws(cmd, s.l);
   vkCmdEndRenderPass(cmd);
   readback(cmd, &s.t, &s.rb);
   end_submit(cmd);
   struct expect e = newexpect(64, 64, CLEAR);
   grid_expect(&e);
   check(&e, s.rb.p, 1, "16 draws, 16 colours");
}

static void r_vbib(void)
{
   struct img t = newtarget(64, 64, 0);
   VkRenderPass rp = newrp(VK_FORMAT_UNDEFINED, VK_IMAGE_LAYOUT_TRANSFER_SRC_OPTIMAL);
   VkFramebuffer fb = newfb(rp, &t, NULL);
   const struct pipe_cfg pc = { SHADER(l_vcol_vert), SHADER(l_vcol_frag), VIN_POS2_COL4, 0, 0, newlayout(0), rp,
                                64, 64 };
   VkPipeline p = newpipe(&pc);
   /* two quads (left half 0.75/0/0.25, right half 0/1/0.5), four vertices each, flat colour per quad */
   const float L[4] = { 0.75f, 0.0f, 0.25f, 1.0f }, R[4] = { 0.0f, 1.0f, 0.5f, 1.0f };
   float v[8][6];
   const float qx[2][2] = { { -1.0f, 0.0f }, { 0.0f, 1.0f } };
   for (int qd = 0; qd < 2; qd++)
      for (int k = 0; k < 4; k++) {
         float *o = v[4 * qd + k];
         o[0] = qx[qd][k & 1];
         o[1] = (k & 2) ? 0.75f : -0.75f;
         memcpy(o + 2, qd ? R : L, 16);
      }
   const uint16_t idx[12] = { 0, 1, 2, 1, 3, 2, 4, 5, 6, 5, 7, 6 };
   struct buf vb = newbuf(sizeof(v), VK_BUFFER_USAGE_VERTEX_BUFFER_BIT);
   struct buf ib = newbuf(sizeof(idx), VK_BUFFER_USAGE_INDEX_BUFFER_BIT);
   struct buf rb = newbuf(64 * 64 * 4, VK_BUFFER_USAGE_TRANSFER_DST_BIT);
   memcpy(vb.p, v, sizeof(v));
   memcpy(ib.p, idx, sizeof(idx));
   VkCommandBuffer cmd = begin(0);
   begin_pass(cmd, rp, fb, 64, 64, CLEAR, 0);
   vkCmdBindPipeline(cmd, VK_PIPELINE_BIND_POINT_GRAPHICS, p);
   const VkDeviceSize off = 0;
   vkCmdBindVertexBuffers(cmd, 0, 1, &vb.b, &off);
   vkCmdBindIndexBuffer(cmd, ib.b, 0, VK_INDEX_TYPE_UINT16);
   vkCmdDrawIndexed(cmd, 12, 1, 0, 0, 0);
   vkCmdEndRenderPass(cmd);
   readback(cmd, &t, &rb);
   end_submit(cmd);
   struct expect e = newexpect(64, 64, CLEAR);
   const float rl[4] = { -1, -0.75f, 0, 0.75f }, rr[4] = { 0, -0.75f, 1, 0.75f };
   expect_rect(&e, rl, L);
   expect_rect(&e, rr, R);
   check(&e, rb.p, 1, "indexed draw, vertex colours");
}

static void r_ubo(void)
{
   VkDescriptorSetLayout dsl = newdsl(VK_DESCRIPTOR_TYPE_UNIFORM_BUFFER, VK_SHADER_STAGE_FRAGMENT_BIT);
   struct rect s = rect_setup(64, 64, SHADER(l_ubo_frag), 0, 0, dsl, 0, VK_IMAGE_LAYOUT_TRANSFER_SRC_OPTIMAL);
   struct buf u = newbuf(256, VK_BUFFER_USAGE_UNIFORM_BUFFER_BIT);
   const float c[4] = { 0.25f, 0.75f, 0.5f, 1.0f };
   memcpy(u.p, c, 16);
   const VkDescriptorBufferInfo bi = { u.b, 0, 16 };
   VkDescriptorSet set = newset(dsl, VK_DESCRIPTOR_TYPE_UNIFORM_BUFFER, &bi, NULL);
   VkCommandBuffer cmd = begin(0);
   rect_begin(cmd, &s, CLEAR);
   vkCmdBindDescriptorSets(cmd, VK_PIPELINE_BIND_POINT_GRAPHICS, s.l, 0, 1, &set, 0, NULL);
   rect_draw(cmd, s.l, FULL, c, 0.5f);
   vkCmdEndRenderPass(cmd);
   readback(cmd, &s.t, &s.rb);
   end_submit(cmd);
   struct expect e = newexpect(64, 64, c);
   check(&e, s.rb.p, 1, "uniform-buffer colour");
}

static void r_blend(void)
{
   struct rect s = rect_setup(64, 64, SHADER(l_pc_frag), 1, 0, 0, 0, VK_IMAGE_LAYOUT_TRANSFER_SRC_OPTIMAL);
   const float dst[4] = { 0.0f, 0.0f, 1.0f, 1.0f }, src[4] = { 1.0f, 0.5f, 0.0f, 0.5f };
   const float r[4] = { -1.0f, -1.0f, 0.0f, 1.0f };
   VkCommandBuffer cmd = begin(0);
   rect_begin(cmd, &s, dst);
   rect_draw(cmd, s.l, r, src, 0.5f);
   vkCmdEndRenderPass(cmd);
   readback(cmd, &s.t, &s.rb);
   end_submit(cmd);
   struct expect e = newexpect(64, 64, dst);
   const float out[4] = { 0.5f, 0.25f, 0.5f, 1.0f }; /* src*a + dst*(1-a); alpha a + 1*(1-a) */
   expect_rect(&e, r, out);
   check(&e, s.rb.p, 2, "SRC_ALPHA / ONE_MINUS_SRC_ALPHA over the left half");
}

static void r_depth(VkFormat f)
{
   /* G5P on the A8 builds D32F and D32F_S8 at one sample (the captured ZLS forms); D16 is refused today */
   skip_refusal = f == VK_FORMAT_D16_UNORM;
   struct rect s = rect_setup(64, 64, SHADER(l_pc_frag), 0, f, 0, 0, VK_IMAGE_LAYOUT_TRANSFER_SRC_OPTIMAL);
   const float c1[4] = { 1, 0, 0, 1 }, c2[4] = { 0, 1, 0, 1 }, c3[4] = { 0, 0.25f, 0.75f, 1 };
   const float left[4] = { -1, -1, 0, 1 };
   VkCommandBuffer cmd = begin(0);
   rect_begin(cmd, &s, CLEAR);
   rect_draw(cmd, s.l, FULL, c1, 0.75f); /* everywhere at 0.75 */
   rect_draw(cmd, s.l, left, c2, 0.25f); /* the left half in front */
   rect_draw(cmd, s.l, FULL, c3, 0.5f);  /* in front of c1, behind c2 */
   vkCmdEndRenderPass(cmd);
   readback(cmd, &s.t, &s.rb);
   end_submit(cmd);
   struct expect e = newexpect(64, 64, c3);
   expect_rect(&e, left, c2);
   check(&e, s.rb.p, 1, f == VK_FORMAT_D32_SFLOAT ? "D32 LESS, 3 layers" : "D16 LESS, 3 layers");
}

/* a 64x64 BGRA8 texture: B = 4x, G = 4y, R = 2(x + y), A = 255 */
static uint8_t tex_val(int x, int y, int c)
{
   return c == 0 ? 4 * x : c == 1 ? 4 * y : c == 2 ? 2 * (x + y) : 255;
}

/* texmap's texels: B = 4x, G = 4y, R = 255 - 2(x + y) (a check), A = 255 */
static uint8_t map_val(int x, int y, int c)
{
   return c == 0 ? 4 * x : c == 1 ? 4 * y : c == 2 ? 255 - 2 * (x + y) : 255;
}

static struct img upload_texture_fn(uint32_t levels, uint8_t (*val)(int, int, int));
static struct img upload_texture(void)
{
   return upload_texture_fn(2, tex_val);
}

static struct img upload_texture_fn(uint32_t levels, uint8_t (*val)(int, int, int))
{
   /* two levels: the twiddled layout every Apple A8 texture capture has (a one-level image would be linear, a
    * Rogue stride state Apple's captures never show); level 0 is filled and sampled (sampler maxLod 0) */
   struct img t = newimg(64, 64, VK_FORMAT_B8G8R8A8_UNORM, VK_IMAGE_USAGE_SAMPLED_BIT | VK_IMAGE_USAGE_TRANSFER_DST_BIT,
                         levels);
   struct buf st = newbuf(64 * 64 * 4, VK_BUFFER_USAGE_TRANSFER_SRC_BIT);
   uint8_t *p = st.p;
   for (int y = 0; y < 64; y++)
      for (int x = 0; x < 64; x++)
         for (int c = 0; c < 4; c++)
            p[4 * (64 * y + x) + c] = val(x, y, c);
   VkCommandBuffer cmd = begin(0);
   image_barrier(cmd, t.i, VK_IMAGE_LAYOUT_UNDEFINED, VK_IMAGE_LAYOUT_TRANSFER_DST_OPTIMAL,
                 VK_PIPELINE_STAGE_TOP_OF_PIPE_BIT, 0, VK_PIPELINE_STAGE_TRANSFER_BIT, VK_ACCESS_TRANSFER_WRITE_BIT);
   const VkBufferImageCopy c = { .imageSubresource = { VK_IMAGE_ASPECT_COLOR_BIT, 0, 0, 1 },
                                 .imageExtent = { 64, 64, 1 } };
   vkCmdCopyBufferToImage(cmd, st.b, t.i, VK_IMAGE_LAYOUT_TRANSFER_DST_OPTIMAL, 1, &c);
   image_barrier(cmd, t.i, VK_IMAGE_LAYOUT_TRANSFER_DST_OPTIMAL, VK_IMAGE_LAYOUT_SHADER_READ_ONLY_OPTIMAL,
                 VK_PIPELINE_STAGE_TRANSFER_BIT, VK_ACCESS_TRANSFER_WRITE_BIT, VK_PIPELINE_STAGE_FRAGMENT_SHADER_BIT,
                 VK_ACCESS_SHADER_READ_BIT);
   end_submit(cmd);
   return t;
}

static VkSampler newsampler(VkFilter f)
{
   const VkSamplerCreateInfo si = { .sType = VK_STRUCTURE_TYPE_SAMPLER_CREATE_INFO, .magFilter = f, .minFilter = f,
                                    .mipmapMode = VK_SAMPLER_MIPMAP_MODE_NEAREST,
                                    .addressModeU = VK_SAMPLER_ADDRESS_MODE_CLAMP_TO_EDGE,
                                    .addressModeV = VK_SAMPLER_ADDRESS_MODE_CLAMP_TO_EDGE,
                                    .addressModeW = VK_SAMPLER_ADDRESS_MODE_CLAMP_TO_EDGE, .maxLod = 0.0f };
   VkSampler s;
   CK(vkCreateSampler(dev, &si, NULL, &s));
   return s;
}

/* draw the texture over the whole target (uv 0..1) and return the readback */
static uint8_t *sample_pass(const struct img *tex, VkFilter filter, uint32_t w, uint32_t h)
{
   VkDescriptorSetLayout dsl = newdsl(VK_DESCRIPTOR_TYPE_COMBINED_IMAGE_SAMPLER, VK_SHADER_STAGE_FRAGMENT_BIT);
   struct rect s = rect_setup(w, h, SHADER(l_tex_frag), 0, 0, dsl, 0, VK_IMAGE_LAYOUT_TRANSFER_SRC_OPTIMAL);
   const VkDescriptorImageInfo ii = { newsampler(filter), tex->v, VK_IMAGE_LAYOUT_SHADER_READ_ONLY_OPTIMAL };
   VkDescriptorSet set = newset(dsl, VK_DESCRIPTOR_TYPE_COMBINED_IMAGE_SAMPLER, NULL, &ii);
   const float black[4] = { 0, 0, 0, 0 };
   VkCommandBuffer cmd = begin(0);
   rect_begin(cmd, &s, black);
   vkCmdBindDescriptorSets(cmd, VK_PIPELINE_BIND_POINT_GRAPHICS, s.l, 0, 1, &set, 0, NULL);
   rect_draw(cmd, s.l, FULL, black, 0.5f);
   vkCmdEndRenderPass(cmd);
   readback(cmd, &s.t, &s.rb);
   end_submit(cmd);
   return s.rb.p;
}

static void r_tex(int linear)
{
   struct img tex = upload_texture();
   const uint32_t n = linear ? 128 : 64;
   uint8_t *got = sample_pass(&tex, linear ? VK_FILTER_LINEAR : VK_FILTER_NEAREST, n, n);
   const float z[4] = { 0, 0, 0, 0 };
   struct expect e = newexpect(n, n, z);
   for (uint32_t y = 0; y < n; y++)
      for (uint32_t x = 0; x < n; x++) {
         uint8_t *o = e.px + 4 * ((size_t)y * n + x);
         if (!linear) {
            for (int c = 0; c < 4; c++)
               o[c] = tex_val(x, y, c);
            continue;
         }
         /* bilinear at texel coordinate (x + 0.5) / 2 - 0.5, clamped to the edge */
         const float u = (x + 0.5f) * 0.5f - 0.5f, v = (y + 0.5f) * 0.5f - 0.5f;
         const int x0 = (int)floorf(u), y0 = (int)floorf(v);
         const float fu = u - x0, fv = v - y0;
         for (int c = 0; c < 4; c++) {
            float acc = 0;
            for (int j = 0; j < 2; j++)
               for (int i = 0; i < 2; i++) {
                  int tx = x0 + i, ty = y0 + j;
                  tx = tx < 0 ? 0 : tx > 63 ? 63 : tx;
                  ty = ty < 0 ? 0 : ty > 63 ? 63 : ty;
                  acc += tex_val(tx, ty, c) * (i ? fu : 1 - fu) * (j ? fv : 1 - fv);
               }
            o[c] = (uint8_t)lroundf(acc);
         }
      }
   check(&e, got, linear ? 3 : 1, linear ? "64x64 texture, LINEAR to 128x128" : "64x64 texture, NEAREST");
}

/* least squares of v ~ a x + b y + c over n points (sums given), printed */
static void fit_print(const char *what, const double s[10], double n)
{
   /* s: sx sy sxx syy sxy sv svx svy (v for one axis), solve the 3x3 normal equations */
   const double m[3][4] = { { s[2], s[4], s[0], s[6] }, { s[4], s[3], s[1], s[7] }, { s[0], s[1], n, s[5] } };
   double a[3][4];
   memcpy(a, m, sizeof(a));
   for (int i = 0; i < 3; i++) {
      int piv = i;
      for (int r = i + 1; r < 3; r++)
         if (fabs(a[r][i]) > fabs(a[piv][i]))
            piv = r;
      for (int c = 0; c < 4; c++) {
         const double t = a[i][c];
         a[i][c] = a[piv][c];
         a[piv][c] = t;
      }
      if (fabs(a[i][i]) < 1e-9) {
         printf("TEXMAP %s fit: singular\n", what);
         return;
      }
      for (int r = 0; r < 3; r++)
         if (r != i) {
            const double f = a[r][i] / a[i][i];
            for (int c = i; c < 4; c++)
               a[r][c] -= f * a[i][c];
         }
   }
   printf("TEXMAP %s = %.3f*px %+.3f*py %+.2f\n", what, a[0][3] / a[0][0], a[1][3] / a[1][1], a[2][3] / a[2][2]);
}

/* texmap: every pixel's source texel. The texture holds (x, y) in its colour (map_val), sampled NEAREST 1:1 over the
 * whole 64x64 target (two triangles, uv from the corners). Prints: counts of correct / wrong-but-a-texel (inside the
 * texture) / not a texel (outside: stale or foreign memory); how many wrong pixels fit a y-flip, an x-flip, a transpose
 * (also a swapped Morton order) or a 180-degree turn; a least-squares affine fit of the source texel per triangle; the
 * first 16 mismatches (px,py)->(tx,ty) or (px,py)->raw. */
static void r_texmap(uint32_t levels)
{
   struct img tex = upload_texture_fn(levels, map_val);
   const uint8_t *got = sample_pass(&tex, VK_FILTER_NEAREST, 64, 64);
   if (nocheck) {
      printf("RESULT %s PASS (not checked) %u level(s)\n", rung, levels);
      finish(0);
   }
   unsigned ok = 0, inside = 0, outside = 0, yflip = 0, xflip = 0, transp = 0, rot = 0, shown = 0;
   unsigned tile_bad[2][2] = { { 0 } }, tri_bad[2] = { 0 }, row_bad[64] = { 0 };
   double fs[2][2][10] = { { { 0 } } }, fn[2] = { 0 }; /* [triangle][axis][sum] */
   char list[16][40];
   for (int py = 0; py < 64; py++)
      for (int px = 0; px < 64; px++) {
         const uint8_t *g = got + 4 * (64 * py + px);
         const int tx = g[0] / 4, ty = g[1] / 4;
         const int valid = !(g[0] & 3) && !(g[1] & 3) && g[2] == 255 - 2 * (tx + ty) && g[3] == 255;
         if (valid && tx == px && ty == py) {
            ok++;
         } else if (valid) {
            inside++;
            yflip += tx == px && ty == 63 - py;
            xflip += tx == 63 - px && ty == py;
            transp += tx == py && ty == px;
            rot += tx == 63 - px && ty == 63 - py;
         } else {
            outside++;
         }
         if (valid) {
            const int tri = (px + 0.5) + (py + 0.5) > 64; /* the rectangle's second triangle */
            for (int axis = 0; axis < 2; axis++) {
               double *f = fs[tri][axis];
               const double v = axis ? ty : tx;
               f[0] += px, f[1] += py, f[2] += px * px, f[3] += py * py, f[4] += px * py;
               f[5] += v, f[6] += v * px, f[7] += v * py;
            }
            fn[tri]++;
         }
         if (!(valid && tx == px && ty == py)) {
            tile_bad[py / 32][px / 32]++;
            tri_bad[(px + 0.5) + (py + 0.5) > 64]++;
            row_bad[py]++;
         }
         if (!(valid && tx == px && ty == py) && shown < 16) {
            if (valid)
               snprintf(list[shown++], sizeof(list[0]), "(%d,%d)->(%d,%d)", px, py, tx, ty);
            else
               snprintf(list[shown++], sizeof(list[0]), "(%d,%d)->raw %02x%02x%02x%02x", px, py, g[0], g[1], g[2],
                        g[3]);
         }
      }
   printf("TEXMAP %u level(s): correct %u, wrong-but-texel (inside) %u, not-a-texel (outside/stale) %u\n", levels, ok,
          inside, outside);
   printf("TEXMAP wrong per 32x32 tile: [%u %u / %u %u], per triangle: first %u second %u\n", tile_bad[0][0],
          tile_bad[0][1], tile_bad[1][0], tile_bad[1][1], tri_bad[0], tri_bad[1]);
   printf("TEXMAP wrong per row:");
   for (int r = 0; r < 64; r++)
      printf(" %u", row_bad[r]);
   printf("\n");
   printf("TEXMAP wrong texels fitting: y-flip %u, x-flip %u, transpose/swapped-Morton %u, 180 %u\n", yflip, xflip,
          transp, rot);
   for (int tri = 0; tri < 2; tri++)
      if (fn[tri] > 3) {
         char w[32];
         snprintf(w, sizeof(w), "triangle %d tx", tri);
         fit_print(w, fs[tri][0], fn[tri]);
         snprintf(w, sizeof(w), "triangle %d ty", tri);
         fit_print(w, fs[tri][1], fn[tri]);
      }
   if (shown) {
      printf("TEXMAP first mismatches:");
      for (unsigned i = 0; i < shown; i++)
         printf(" %s", list[i]);
      printf("\n");
   }
   if (!inside && !outside) {
      printf("RESULT %s PASS 4096/4096 pixels took their own texel (%u level(s))\n", rung, levels);
      finish(0);
   }
   printf("RESULT %s FAIL %u/4096 wrong: %u wrong texel, %u not a texel (%u level(s))\n", rung, inside + outside,
          inside, outside, levels);
   finish(1);
}

/* varyings: a 64x64 rectangle of two triangles from four vertices of four colours (indexed), interpolated per pixel,
 * against the CPU's barycentric interpolation (3 steps per channel of tolerance) */
static void r_varyings(void)
{
   struct img t = newtarget(64, 64, 0);
   VkRenderPass rp = newrp(VK_FORMAT_UNDEFINED, VK_IMAGE_LAYOUT_TRANSFER_SRC_OPTIMAL);
   VkFramebuffer fb = newfb(rp, &t, NULL);
   const struct pipe_cfg pc = { SHADER(l_vcol_vert), SHADER(l_vcol_frag), VIN_POS2_COL4, 0, 0, newlayout(0), rp,
                                64, 64 };
   VkPipeline p = newpipe(&pc);
   static const float v[4][6] = {
      { -1, -1, 1.0f, 0.0f, 0.0f, 1.0f }, /* top left: red */
      { 1, -1, 0.0f, 1.0f, 0.0f, 1.0f },  /* top right: green */
      { -1, 1, 0.0f, 0.0f, 1.0f, 1.0f },  /* bottom left: blue */
      { 1, 1, 1.0f, 1.0f, 1.0f, 1.0f },   /* bottom right: white */
   };
   const uint16_t idx[6] = { 0, 1, 2, 1, 3, 2 };
   struct buf vb = newbuf(sizeof(v), VK_BUFFER_USAGE_VERTEX_BUFFER_BIT);
   struct buf ib = newbuf(sizeof(idx), VK_BUFFER_USAGE_INDEX_BUFFER_BIT);
   struct buf rb = newbuf(64 * 64 * 4, VK_BUFFER_USAGE_TRANSFER_DST_BIT);
   memcpy(vb.p, v, sizeof(v));
   memcpy(ib.p, idx, sizeof(idx));
   VkCommandBuffer cmd = begin(0);
   begin_pass(cmd, rp, fb, 64, 64, CLEAR, 0);
   vkCmdBindPipeline(cmd, VK_PIPELINE_BIND_POINT_GRAPHICS, p);
   const VkDeviceSize off = 0;
   vkCmdBindVertexBuffers(cmd, 0, 1, &vb.b, &off);
   vkCmdBindIndexBuffer(cmd, ib.b, 0, VK_INDEX_TYPE_UINT16);
   vkCmdDrawIndexed(cmd, 6, 1, 0, 0, 0);
   vkCmdEndRenderPass(cmd);
   readback(cmd, &t, &rb);
   end_submit(cmd);
   struct expect e = newexpect(64, 64, CLEAR);
   for (int py = 0; py < 64; py++)
      for (int px = 0; px < 64; px++) {
         /* (u, v) in [0,1]: triangle 0 (v0 v1 v2) where u + v < 1, else triangle 1 (v1 v3 v2) */
         const float u = (px + 0.5f) / 64, w = (py + 0.5f) / 64;
         float c[4];
         for (int k = 0; k < 4; k++)
            c[k] = u + w < 1 ? v[0][2 + k] * (1 - u - w) + v[1][2 + k] * u + v[2][2 + k] * w
                             : v[1][2 + k] * (1 - w) + v[3][2 + k] * (u + w - 1) + v[2][2 + k] * (1 - u);
         bgra(e.px + 4 * (64 * py + px), c);
      }
   check(&e, rb.p, 3, "four vertex colours interpolated over two triangles");
}

/* ---- round 2: more texture coverage, LOAD ---- */

/* a w x h texture of `levels` levels (viewed as view_levels), format f; val(x, y, byte, level) gives its bytes in memory
 * order. Filled through one staging buffer and vkCmdCopyBufferToImage per level, as applications upload. */
static struct img upload_tex(uint32_t w, uint32_t h, VkFormat f, uint32_t levels, uint32_t view_levels,
                             uint8_t (*val)(int, int, int, int))
{
   struct img t = newimg_ex(w, h, f, VK_IMAGE_USAGE_SAMPLED_BIT | VK_IMAGE_USAGE_TRANSFER_DST_BIT, levels,
                            view_levels, 0);
   VkDeviceSize total = 0;
   for (uint32_t l = 0; l < levels; l++)
      total += (VkDeviceSize)MAX2(w >> l, 1u) * MAX2(h >> l, 1u) * 4;
   struct buf st = newbuf(total, VK_BUFFER_USAGE_TRANSFER_SRC_BIT);
   VkBufferImageCopy reg[16];
   VkDeviceSize off = 0;
   for (uint32_t l = 0; l < levels; l++) {
      const uint32_t lw = MAX2(w >> l, 1u), lh = MAX2(h >> l, 1u);
      uint8_t *p = (uint8_t *)st.p + off;
      for (uint32_t y = 0; y < lh; y++)
         for (uint32_t x = 0; x < lw; x++)
            for (int c = 0; c < 4; c++)
               p[4 * (lw * y + x) + c] = val(x, y, c, l);
      reg[l] = (VkBufferImageCopy){ .bufferOffset = off, .imageSubresource = { VK_IMAGE_ASPECT_COLOR_BIT, l, 0, 1 },
                                    .imageExtent = { lw, lh, 1 } };
      off += (VkDeviceSize)lw * lh * 4;
   }
   VkCommandBuffer cmd = begin(0);
   image_barrier(cmd, t.i, VK_IMAGE_LAYOUT_UNDEFINED, VK_IMAGE_LAYOUT_TRANSFER_DST_OPTIMAL,
                 VK_PIPELINE_STAGE_TOP_OF_PIPE_BIT, 0, VK_PIPELINE_STAGE_TRANSFER_BIT, VK_ACCESS_TRANSFER_WRITE_BIT);
   vkCmdCopyBufferToImage(cmd, st.b, t.i, VK_IMAGE_LAYOUT_TRANSFER_DST_OPTIMAL, levels, reg);
   image_barrier(cmd, t.i, VK_IMAGE_LAYOUT_TRANSFER_DST_OPTIMAL, VK_IMAGE_LAYOUT_SHADER_READ_ONLY_OPTIMAL,
                 VK_PIPELINE_STAGE_TRANSFER_BIT, VK_ACCESS_TRANSFER_WRITE_BIT, VK_PIPELINE_STAGE_FRAGMENT_SHADER_BIT,
                 VK_ACCESS_SHADER_READ_BIT);
   end_submit(cmd);
   return t;
}

static VkSampler newsampler_ex(VkFilter f, VkSamplerMipmapMode mip, VkSamplerAddressMode am, float max_lod)
{
   const VkSamplerCreateInfo si = { .sType = VK_STRUCTURE_TYPE_SAMPLER_CREATE_INFO, .magFilter = f, .minFilter = f,
                                    .mipmapMode = mip, .addressModeU = am, .addressModeV = am, .addressModeW = am,
                                    .maxLod = max_lod };
   VkSampler s;
   CK(vkCreateSampler(dev, &si, NULL, &s));
   return s;
}

/* the texture over the whole w x h target with uv 0..(su, sv); the readback */
static uint8_t *sample_pass_ex(const struct img *tex, VkSampler smp, uint32_t w, uint32_t h, float su, float sv)
{
   VkDescriptorSetLayout dsl = newdsl(VK_DESCRIPTOR_TYPE_COMBINED_IMAGE_SAMPLER, VK_SHADER_STAGE_FRAGMENT_BIT);
   struct rect s = rect_setup(w, h, SHADER(l_tex_frag), 0, 0, dsl, 0, VK_IMAGE_LAYOUT_TRANSFER_SRC_OPTIMAL);
   const VkDescriptorImageInfo ii = { smp, tex->v, VK_IMAGE_LAYOUT_SHADER_READ_ONLY_OPTIMAL };
   VkDescriptorSet set = newset(dsl, VK_DESCRIPTOR_TYPE_COMBINED_IMAGE_SAMPLER, NULL, &ii);
   const float black[4] = { 0, 0, 0, 0 };
   VkCommandBuffer cmd = begin(0);
   rect_begin(cmd, &s, black);
   vkCmdBindDescriptorSets(cmd, VK_PIPELINE_BIND_POINT_GRAPHICS, s.l, 0, 1, &set, 0, NULL);
   rect_draw_uv(cmd, s.l, FULL, black, 0.5f, su, sv);
   vkCmdEndRenderPass(cmd);
   readback(cmd, &s.t, &s.rb);
   end_submit(cmd);
   return s.rb.p;
}

/* texmap's encoding in memory order B,G,R,A (a BGRA8 texture) */
static uint8_t map4(int x, int y, int c, int l)
{
   (void)l;
   return map_val(x, y, c);
}

/* the same texels in memory order R,G,B,A (an RGBA8 texture): r = 4x, g = 4y, b = check, a */
static uint8_t map4_rgba(int x, int y, int c, int l)
{
   (void)l;
   return c == 0 ? 4 * x : c == 1 ? 4 * y : c == 2 ? 255 - 2 * (x + y) : 255;
}

/* a non-power-of-two texture's texels (x < 128, y < 64): B = 2x, G = 4y, R = 255 - (x + y), A */
static uint8_t map_npot(int x, int y, int c, int l)
{
   (void)l;
   return c == 0 ? 2 * x : c == 1 ? 4 * y : c == 2 ? 255 - (x + y) : 255;
}

/* level l of texmip's texture: one colour per level */
static void mip_colour(int l, uint8_t out[4])
{
   out[0] = (uint8_t)(36 * l);
   out[1] = (uint8_t)(255 - 36 * l);
   out[2] = (uint8_t)(l & 1 ? 0x30 : 0xc0);
   out[3] = 255;
}

static uint8_t map_mip(int x, int y, int c, int l)
{
   (void)x, (void)y;
   uint8_t col[4];
   mip_colour(l, col);
   return col[c];
}

static void expect_bytes(struct expect *e, int px, int py, const uint8_t b[4])
{
   memcpy(e->px + 4 * ((size_t)py * e->w + px), b, 4);
}

static void r_texrgba(void)
{
   struct img tex = upload_tex(64, 64, VK_FORMAT_R8G8B8A8_UNORM, 2, 1, map4_rgba);
   const uint8_t *got = sample_pass_ex(&tex, newsampler_ex(VK_FILTER_NEAREST, VK_SAMPLER_MIPMAP_MODE_NEAREST,
                                                           VK_SAMPLER_ADDRESS_MODE_CLAMP_TO_EDGE, 0), 64, 64, 1, 1);
   const float z[4] = { 0 };
   struct expect e = newexpect(64, 64, z);
   for (int y = 0; y < 64; y++)
      for (int x = 0; x < 64; x++) {
         /* the BGRA8 target stores (b, g, r, a) of the sampled (r, g, b, a) */
         const uint8_t b[4] = { map4_rgba(x, y, 2, 0), map4_rgba(x, y, 1, 0), map4_rgba(x, y, 0, 0), 255 };
         expect_bytes(&e, x, y, b);
      }
   check(&e, got, 1, "an RGBA8 texture (R = 4x, G = 4y) into a BGRA8 target, NEAREST 1:1");
}

static void r_texnpot(uint32_t levels)
{
   struct img tex = upload_tex(100, 60, VK_FORMAT_B8G8R8A8_UNORM, levels, 1, map_npot);
   const uint8_t *got = sample_pass_ex(&tex, newsampler_ex(VK_FILTER_NEAREST, VK_SAMPLER_MIPMAP_MODE_NEAREST,
                                                           VK_SAMPLER_ADDRESS_MODE_CLAMP_TO_EDGE, 0), 100, 60, 1, 1);
   const float z[4] = { 0 };
   struct expect e = newexpect(100, 60, z);
   for (int y = 0; y < 60; y++)
      for (int x = 0; x < 100; x++) {
         const uint8_t b[4] = { map_npot(x, y, 0, 0), map_npot(x, y, 1, 0), map_npot(x, y, 2, 0), 255 };
         expect_bytes(&e, x, y, b);
      }
   check(&e, got, 1, levels > 1 ? "100x60 texture (twiddled), NEAREST 1:1" : "100x60 texture (one level, linear)");
}

/* REPEAT / MIRRORED_REPEAT: uv 0..1.5 over 64 pixels; pixel p samples texel floor((p + 0.5) * 1.5) wrapped */
static int wrap_texel(int p, int mirror)
{
   const int c = (int)floorf((p + 0.5f) * 1.5f);
   if (!mirror)
      return c % 64;
   const int m = c % 128;
   return m < 64 ? m : 127 - m;
}

static void r_texwrap(int mirror)
{
   struct img tex = upload_tex(64, 64, VK_FORMAT_B8G8R8A8_UNORM, 2, 1, map4);
   const uint8_t *got = sample_pass_ex(
      &tex, newsampler_ex(VK_FILTER_NEAREST, VK_SAMPLER_MIPMAP_MODE_NEAREST,
                          mirror ? VK_SAMPLER_ADDRESS_MODE_MIRRORED_REPEAT : VK_SAMPLER_ADDRESS_MODE_REPEAT, 0),
      64, 64, 1.5f, 1.5f);
   const float z[4] = { 0 };
   struct expect e = newexpect(64, 64, z);
   for (int y = 0; y < 64; y++)
      for (int x = 0; x < 64; x++) {
         const int tx = wrap_texel(x, mirror), ty = wrap_texel(y, mirror);
         const uint8_t b[4] = { map_val(tx, ty, 0), map_val(tx, ty, 1), map_val(tx, ty, 2), 255 };
         expect_bytes(&e, x, y, b);
      }
   check(&e, got, 1, mirror ? "MIRRORED_REPEAT, uv 0..1.5" : "REPEAT, uv 0..1.5");
}

/* texmip: a 64x64 texture of 7 levels, one colour each, NEAREST/NEAREST-mip into 64, 32, 16, 8 and 4 pixel targets:
 * LOD 0..4. (G5P lays mips out as the A9 does, twiddle grids padded to at least 32x32; Rogue's TPU computes level
 * offsets from the unpadded sizes, which agree through level 2 and diverge from level 3.) */
static void r_texmip(void)
{
   struct img tex = upload_tex(64, 64, VK_FORMAT_B8G8R8A8_UNORM, 7, 7, map_mip);
   VkSampler smp = newsampler_ex(VK_FILTER_NEAREST, VK_SAMPLER_MIPMAP_MODE_NEAREST,
                                 VK_SAMPLER_ADDRESS_MODE_CLAMP_TO_EDGE, 16.0f);
   static const uint32_t size[5] = { 64, 32, 16, 8, 4 };
   unsigned bad[5] = { 0 }, got_lvl[5];
   uint8_t first_px[5][4] = { { 0 } };
   for (int i = 0; i < 5; i++) {
      const uint8_t *got = sample_pass_ex(&tex, smp, size[i], size[i], 1, 1);
      uint8_t want[4];
      mip_colour(i, want);
      got_lvl[i] = 99;
      for (uint32_t k = 0; k < size[i] * size[i]; k++) {
         int off = 0;
         for (int c = 0; c < 4; c++)
            off |= abs(got[4 * k + c] - want[c]) > 1;
         if (off && !bad[i]++) {
            memcpy(first_px[i], got + 4 * k, 4);
            for (int l = 0; l < 7; l++) {
               uint8_t lc[4];
               mip_colour(l, lc);
               if (!memcmp(lc, got + 4 * k, 4))
                  got_lvl[i] = l;
            }
         }
      }
   }
   if (nocheck) {
      printf("RESULT %s PASS (not checked)\n", rung);
      finish(0);
   }
   char d[512];
   int n = 0;
   for (int i = 0; i < 5; i++)
      n += snprintf(d + n, sizeof(d) - n, " LOD%d %u/%u%s", i, bad[i], size[i] * size[i], bad[i] ? "" : " ok");
   if (!bad[0] && !bad[1] && !bad[2] && !bad[3] && !bad[4]) {
      printf("RESULT %s PASS LOD 0..4 each took its level (64..4 pixel targets, every pixel)\n", rung);
      finish(0);
   }
   printf("RESULT %s FAIL wrong pixels:%s; first wrong per LOD:", rung, d);
   for (int i = 0; i < 5; i++)
      if (bad[i])
         printf(" LOD%d %02x%02x%02x%02x (level %u)", i, first_px[i][0], first_px[i][1], first_px[i][2],
                first_px[i][3], got_lvl[i]);
   printf(" (level 99 = no level's colour)\n");
   finish(1);
}

/* texhostlinear: a VK_IMAGE_TILING_LINEAR image in host-visible memory, written by the CPU at its row pitch, sampled */
static void r_texhostlinear(void)
{
   struct img tex = newimg_ex(64, 64, VK_FORMAT_B8G8R8A8_UNORM, VK_IMAGE_USAGE_SAMPLED_BIT, 1, 1, 1);
   const VkImageSubresource sub = { VK_IMAGE_ASPECT_COLOR_BIT, 0, 0 };
   VkSubresourceLayout lay;
   vkGetImageSubresourceLayout(dev, tex.i, &sub, &lay);
   uint8_t *map;
   CK(vkMapMemory(dev, tex.m, 0, VK_WHOLE_SIZE, 0, (void **)&map));
   for (int y = 0; y < 64; y++)
      for (int x = 0; x < 64; x++)
         for (int c = 0; c < 4; c++)
            map[lay.offset + y * lay.rowPitch + 4 * x + c] = map_val(x, y, c);
   VkCommandBuffer cmd = begin(0);
   image_barrier(cmd, tex.i, VK_IMAGE_LAYOUT_PREINITIALIZED, VK_IMAGE_LAYOUT_SHADER_READ_ONLY_OPTIMAL,
                 VK_PIPELINE_STAGE_HOST_BIT, VK_ACCESS_HOST_WRITE_BIT, VK_PIPELINE_STAGE_FRAGMENT_SHADER_BIT,
                 VK_ACCESS_SHADER_READ_BIT);
   end_submit(cmd);
   const uint8_t *got = sample_pass_ex(&tex, newsampler_ex(VK_FILTER_NEAREST, VK_SAMPLER_MIPMAP_MODE_NEAREST,
                                                           VK_SAMPLER_ADDRESS_MODE_CLAMP_TO_EDGE, 0), 64, 64, 1, 1);
   const float z[4] = { 0 };
   struct expect e = newexpect(64, 64, z);
   for (int y = 0; y < 64; y++)
      for (int x = 0; x < 64; x++) {
         const uint8_t b[4] = { map_val(x, y, 0), map_val(x, y, 1), map_val(x, y, 2), 255 };
         expect_bytes(&e, x, y, b);
      }
   char d[96];
   snprintf(d, sizeof(d), "linear-tiling host image, row pitch %u, offset %u", (unsigned)lay.rowPitch,
            (unsigned)lay.offset);
   check(&e, got, 1, d);
}

/* tex2: two textures in one fragment program (left half t0, right half t1) */
static uint8_t solid_a(int x, int y, int c, int l)
{
   (void)x, (void)y, (void)l;
   static const uint8_t v[4] = { 0x40, 0xc0, 0x20, 0xff };
   return v[c];
}

static uint8_t solid_b(int x, int y, int c, int l)
{
   (void)x, (void)y, (void)l;
   static const uint8_t v[4] = { 0xe0, 0x10, 0x90, 0xff };
   return v[c];
}

static void r_tex2(void)
{
   /* the A8 sample names one image slot (common dword 0) in every Apple capture: a second texture is refused */
   skip_refusal = 1;
   struct img a = upload_tex(64, 64, VK_FORMAT_B8G8R8A8_UNORM, 2, 1, solid_a);
   struct img b = upload_tex(64, 64, VK_FORMAT_B8G8R8A8_UNORM, 2, 1, solid_b);
   const VkDescriptorSetLayoutBinding bd[2] = {
      { 0, VK_DESCRIPTOR_TYPE_COMBINED_IMAGE_SAMPLER, 1, VK_SHADER_STAGE_FRAGMENT_BIT },
      { 1, VK_DESCRIPTOR_TYPE_COMBINED_IMAGE_SAMPLER, 1, VK_SHADER_STAGE_FRAGMENT_BIT } };
   const VkDescriptorSetLayoutCreateInfo li = { .sType = VK_STRUCTURE_TYPE_DESCRIPTOR_SET_LAYOUT_CREATE_INFO,
                                                .bindingCount = 2, .pBindings = bd };
   VkDescriptorSetLayout dsl;
   CK(vkCreateDescriptorSetLayout(dev, &li, NULL, &dsl));
   struct rect s = rect_setup(64, 64, SHADER(l_tex2_frag), 0, 0, dsl, 0, VK_IMAGE_LAYOUT_TRANSFER_SRC_OPTIMAL);
   const VkDescriptorPoolSize ps = { VK_DESCRIPTOR_TYPE_COMBINED_IMAGE_SAMPLER, 2 };
   const VkDescriptorPoolCreateInfo pi = { .sType = VK_STRUCTURE_TYPE_DESCRIPTOR_POOL_CREATE_INFO, .maxSets = 1,
                                           .poolSizeCount = 1, .pPoolSizes = &ps };
   VkDescriptorPool dp;
   CK(vkCreateDescriptorPool(dev, &pi, NULL, &dp));
   const VkDescriptorSetAllocateInfo ai = { .sType = VK_STRUCTURE_TYPE_DESCRIPTOR_SET_ALLOCATE_INFO,
                                            .descriptorPool = dp, .descriptorSetCount = 1, .pSetLayouts = &dsl };
   VkDescriptorSet set;
   CK(vkAllocateDescriptorSets(dev, &ai, &set));
   VkSampler smp = newsampler_ex(VK_FILTER_NEAREST, VK_SAMPLER_MIPMAP_MODE_NEAREST,
                                 VK_SAMPLER_ADDRESS_MODE_CLAMP_TO_EDGE, 0);
   const VkDescriptorImageInfo ii[2] = { { smp, a.v, VK_IMAGE_LAYOUT_SHADER_READ_ONLY_OPTIMAL },
                                         { smp, b.v, VK_IMAGE_LAYOUT_SHADER_READ_ONLY_OPTIMAL } };
   const VkWriteDescriptorSet w[2] = {
      { .sType = VK_STRUCTURE_TYPE_WRITE_DESCRIPTOR_SET, .dstSet = set, .dstBinding = 0, .descriptorCount = 1,
        .descriptorType = VK_DESCRIPTOR_TYPE_COMBINED_IMAGE_SAMPLER, .pImageInfo = &ii[0] },
      { .sType = VK_STRUCTURE_TYPE_WRITE_DESCRIPTOR_SET, .dstSet = set, .dstBinding = 1, .descriptorCount = 1,
        .descriptorType = VK_DESCRIPTOR_TYPE_COMBINED_IMAGE_SAMPLER, .pImageInfo = &ii[1] } };
   vkUpdateDescriptorSets(dev, 2, w, 0, NULL);
   const float black[4] = { 0 };
   VkCommandBuffer cmd = begin(0);
   rect_begin(cmd, &s, black);
   vkCmdBindDescriptorSets(cmd, VK_PIPELINE_BIND_POINT_GRAPHICS, s.l, 0, 1, &set, 0, NULL);
   rect_draw(cmd, s.l, FULL, black, 0.5f);
   vkCmdEndRenderPass(cmd);
   readback(cmd, &s.t, &s.rb);
   end_submit(cmd);
   skip_refusal = 0;
   struct expect e = newexpect(64, 64, black);
   for (int y = 0; y < 64; y++)
      for (int x = 0; x < 64; x++) {
         const uint8_t v[4] = { x < 32 ? 0x40 : 0xe0, x < 32 ? 0xc0 : 0x10, x < 32 ? 0x20 : 0x90, 0xff };
         expect_bytes(&e, x, y, v);
      }
   check(&e, s.rb.p, 1, "two textures in one fragment program");
}

/* LOAD: pass 1 clears and draws the left half; pass 2 (loadOp LOAD) draws the top half; pass 3 (LOAD) a centre box.
 * One command buffer, one render pass per pass; the result is every pass composed in order. */
static void r_load(uint32_t w, uint32_t h, int passes)
{
   struct img t = newtarget(w, h, 0);
   VkRenderPass rp1 = newrp_ex(0, VK_ATTACHMENT_LOAD_OP_CLEAR, VK_IMAGE_LAYOUT_UNDEFINED,
                               passes > 1 ? VK_IMAGE_LAYOUT_COLOR_ATTACHMENT_OPTIMAL : VK_IMAGE_LAYOUT_TRANSFER_SRC_OPTIMAL);
   VkRenderPass rpm = newrp_ex(0, VK_ATTACHMENT_LOAD_OP_LOAD, VK_IMAGE_LAYOUT_COLOR_ATTACHMENT_OPTIMAL,
                               VK_IMAGE_LAYOUT_COLOR_ATTACHMENT_OPTIMAL);
   VkRenderPass rpl = newrp_ex(0, VK_ATTACHMENT_LOAD_OP_LOAD, VK_IMAGE_LAYOUT_COLOR_ATTACHMENT_OPTIMAL,
                               VK_IMAGE_LAYOUT_TRANSFER_SRC_OPTIMAL);
   VkFramebuffer fb = newfb(rp1, &t, NULL);
   VkPipelineLayout l = newlayout(0);
   const struct pipe_cfg pc = { SHADER(l_rect_vert), SHADER(l_pc_frag), VIN_CORNER2, 0, 0, l, rp1, w, h };
   VkPipeline p = newpipe(&pc);
   struct buf vb = newbuf(sizeof(CORNERS), VK_BUFFER_USAGE_VERTEX_BUFFER_BIT);
   memcpy(vb.p, CORNERS, sizeof(CORNERS));
   struct buf rb = newbuf((VkDeviceSize)w * h * 4, VK_BUFFER_USAGE_TRANSFER_DST_BIT);
   const float c0[4] = { 0.0f, 0.0f, 0.25f, 1.0f }, c1[4] = { 1.0f, 0.25f, 0.0f, 1.0f },
               c2[4] = { 0.0f, 0.75f, 0.25f, 1.0f }, c3[4] = { 0.75f, 0.75f, 1.0f, 1.0f };
   const float ra[4] = { -1, -1, 0, 1 }, rbx[4] = { -1, -1, 1, 0 }, rc[4] = { -0.4f, -0.4f, 0.4f, 0.4f };
   const float *rects[3] = { ra, rbx, rc }, *cols[3] = { c1, c2, c3 };
   VkCommandBuffer cmd = begin(0);
   for (int i = 0; i < passes; i++) {
      VkRenderPass rp = !i ? rp1 : i == passes - 1 ? rpl : rpm;
      begin_pass(cmd, rp, fb, w, h, c0, 0);
      vkCmdBindPipeline(cmd, VK_PIPELINE_BIND_POINT_GRAPHICS, p);
      const VkDeviceSize off = 0;
      vkCmdBindVertexBuffers(cmd, 0, 1, &vb.b, &off);
      rect_draw(cmd, l, rects[i], cols[i], 0.5f);
      vkCmdEndRenderPass(cmd);
      if (i < passes - 1)
         barrier(cmd, VK_PIPELINE_STAGE_COLOR_ATTACHMENT_OUTPUT_BIT, VK_ACCESS_COLOR_ATTACHMENT_WRITE_BIT,
                 VK_PIPELINE_STAGE_FRAGMENT_SHADER_BIT | VK_PIPELINE_STAGE_COLOR_ATTACHMENT_OUTPUT_BIT,
                 VK_ACCESS_COLOR_ATTACHMENT_READ_BIT | VK_ACCESS_COLOR_ATTACHMENT_WRITE_BIT |
                    VK_ACCESS_INPUT_ATTACHMENT_READ_BIT);
   }
   readback(cmd, &t, &rb);
   end_submit(cmd);
   struct expect e = newexpect(w, h, c0);
   for (int i = 0; i < passes; i++)
      expect_rect(&e, rects[i], cols[i]);
   char d[96];
   snprintf(d, sizeof(d), "%d passes (clear, then LOAD), job %.1f ms", passes, last_ms);
   check(&e, rb.p, 1, d);
}

/* mesh / meshvary: one draw of N triangles at 256x256, N = 1k, 2k, 4k, 8k, 16k, 32k, 64k, each its own submission.
 * The triangles tile the target exactly (32 columns x N/64 rows of cells, two triangles each), so every pixel ends
 * the triangle colour. meshvary adds a vec4 varying per vertex (more parameter memory per vertex, as vkmark's lit
 * meshes have). Prints one MESH line per step; the step that times out is the size curve's knee. */
static void r_mesh(int vary)
{
   const uint32_t w = 256, h = 256;
   struct img t = newtarget(w, h, 0);
   VkRenderPass rp = newrp(VK_FORMAT_UNDEFINED, VK_IMAGE_LAYOUT_TRANSFER_SRC_OPTIMAL);
   VkFramebuffer fb = newfb(rp, &t, NULL);
   const struct pipe_cfg pc = { vary ? SHADER(l_vcol_vert) : SHADER(l_tri_vert),
                                vary ? SHADER(l_vcol_frag) : SHADER(l_const_frag),
                                vary ? VIN_POS2_COL4 : VIN_POS4, 0, 0, newlayout(0), rp, w, h };
   VkPipeline p = newpipe(&pc);
   const unsigned maxn = 65536, stride = vary ? 24 : 16;
   struct buf vb = newbuf((VkDeviceSize)maxn * 3 * stride, VK_BUFFER_USAGE_VERTEX_BUFFER_BIT);
   struct buf rb = newbuf(w * h * 4, VK_BUFFER_USAGE_TRANSFER_DST_BIT);
   struct expect e = newexpect(w, h, TRI);
   char curve[256] = "";
   int cl = 0;
   for (unsigned n = 1024; n <= maxn; n *= 2) {
      const unsigned cols = 32, rows = n / 64;
      float *v = vb.p;
      for (unsigned r = 0; r < rows; r++)
         for (unsigned col = 0; col < cols; col++) {
            const float x0 = -1 + 2.0f * col / cols, x1 = -1 + 2.0f * (col + 1) / cols;
            const float y0 = -1 + 2.0f * r / rows, y1 = -1 + 2.0f * (r + 1) / rows;
            const float q[6][2] = { { x0, y0 }, { x1, y0 }, { x0, y1 }, { x1, y0 }, { x1, y1 }, { x0, y1 } };
            for (int k = 0; k < 6; k++) {
               *v++ = q[k][0];
               *v++ = q[k][1];
               if (vary) {
                  memcpy(v, TRI, 16);
                  v += 4;
               } else {
                  *v++ = 0;
                  *v++ = 1;
               }
            }
         }
      step("mesh %u triangles", n);
      memset(rb.p, 0, w * h * 4);
      VkCommandBuffer cmd = begin(0);
      begin_pass(cmd, rp, fb, w, h, CLEAR, 0);
      vkCmdBindPipeline(cmd, VK_PIPELINE_BIND_POINT_GRAPHICS, p);
      const VkDeviceSize off = 0;
      vkCmdBindVertexBuffers(cmd, 0, 1, &vb.b, &off);
      vkCmdDraw(cmd, 3 * n, 1, 0, 0);
      vkCmdEndRenderPass(cmd);
      readback(cmd, &t, &rb);
      end_submit(cmd);
      size_t bad = 0;
      for (size_t i = 0; !nocheck && i < w * h; i++)
         for (int c = 0; c < 4; c++)
            if (abs(((uint8_t *)rb.p)[4 * i + c] - e.px[4 * i + c]) > 1) {
               bad++;
               break;
            }
      printf("MESH %s %u triangles: %s %.1f ms\n", vary ? "vec4-varying" : "plain", n,
             bad ? "WRONG" : nocheck ? "done" : "ok", last_ms);
      cl += snprintf(curve + cl, sizeof(curve) - cl, " %uk:%.0fms%s", n / 1024, last_ms, bad ? "(wrong)" : "");
      if (bad) {
         printf("RESULT %s FAIL at %u triangles: %zu/%u pixels wrong; curve%s\n", rung, n, bad, w * h, curve);
         finish(1);
      }
   }
   printf("RESULT %s PASS 1k..64k triangles at 256x256, every pixel; curve%s\n", rung, curve);
   finish(0);
}

/* texmath: two samples whose 32-bit results feed arithmetic (the sample form vkmark's texture, effect2d and desktop
 * scenes need): each pixel the average of its texel and the right neighbour's, texmap's 64x64 texture, NEAREST */
static void r_texmath(void)
{
   struct img tex = upload_tex(64, 64, VK_FORMAT_B8G8R8A8_UNORM, 2, 1, map4);
   VkDescriptorSetLayout dsl = newdsl(VK_DESCRIPTOR_TYPE_COMBINED_IMAGE_SAMPLER, VK_SHADER_STAGE_FRAGMENT_BIT);
   struct rect s = rect_setup(64, 64, SHADER(l_texmath_frag), 0, 0, dsl, 0, VK_IMAGE_LAYOUT_TRANSFER_SRC_OPTIMAL);
   const VkDescriptorImageInfo ii = { newsampler_ex(VK_FILTER_NEAREST, VK_SAMPLER_MIPMAP_MODE_NEAREST,
                                                    VK_SAMPLER_ADDRESS_MODE_CLAMP_TO_EDGE, 0),
                                      tex.v, VK_IMAGE_LAYOUT_SHADER_READ_ONLY_OPTIMAL };
   VkDescriptorSet set = newset(dsl, VK_DESCRIPTOR_TYPE_COMBINED_IMAGE_SAMPLER, NULL, &ii);
   const float black[4] = { 0 };
   VkCommandBuffer cmd = begin(0);
   rect_begin(cmd, &s, black);
   vkCmdBindDescriptorSets(cmd, VK_PIPELINE_BIND_POINT_GRAPHICS, s.l, 0, 1, &set, 0, NULL);
   rect_draw(cmd, s.l, FULL, black, 0.5f);
   vkCmdEndRenderPass(cmd);
   readback(cmd, &s.t, &s.rb);
   end_submit(cmd);
   struct expect e = newexpect(64, 64, black);
   for (int y = 0; y < 64; y++)
      for (int x = 0; x < 64; x++) {
         const int x1 = x < 63 ? x + 1 : 63;
         uint8_t b[4];
         for (int c = 0; c < 4; c++)
            b[c] = (uint8_t)((map_val(x, y, c) + map_val(x1, y, c) + 1) / 2);
         expect_bytes(&e, x, y, b);
      }
   check(&e, s.rb.p, 2, "0.5 * texel + 0.5 * right neighbour (32-bit sample results into arithmetic)");
}

/* litmesh: a lat/long sphere with normals and per-vertex diffuse lighting, depth-tested (D32), at 256x256, drawn
 * twice: both frames must be identical, inside the silhouette lit (never the clear colour), outside the clear
 * colour (a 2-pixel band at the outline is not judged). vkmark vertex/shading without texturing; the pool-grow
 * regression test (litmesh about 3k triangles, below the plain mesh's 8k knee; litmeshbig about 24k). */
static void r_litmesh(unsigned su, unsigned sv)
{
   const uint32_t w = 256, h = 256;
   const unsigned ntri = 2 * su * sv;
   struct img t = newtarget(w, h, 0);
   struct img d = newimg(w, h, VK_FORMAT_D32_SFLOAT, VK_IMAGE_USAGE_DEPTH_STENCIL_ATTACHMENT_BIT, 1);
   VkRenderPass rp = newrp(VK_FORMAT_D32_SFLOAT, VK_IMAGE_LAYOUT_TRANSFER_SRC_OPTIMAL);
   VkFramebuffer fb = newfb(rp, &t, &d);
   const struct pipe_cfg pc = { SHADER(l_lit_vert), SHADER(l_vcol_frag), VIN_POS3_NRM3, 0, 1, newlayout(0), rp, w, h };
   VkPipeline p = newpipe(&pc);
   struct buf vb = newbuf((VkDeviceSize)ntri * 3 * 24, VK_BUFFER_USAGE_VERTEX_BUFFER_BIT);
   struct buf rb[2] = { newbuf(w * h * 4, VK_BUFFER_USAGE_TRANSFER_DST_BIT), newbuf(w * h * 4, VK_BUFFER_USAGE_TRANSFER_DST_BIT) };
   float *v = vb.p;
   for (unsigned i = 0; i < su; i++)
      for (unsigned j = 0; j < sv; j++) {
         float q[4][3];
         for (int k = 0; k < 4; k++) {
            const unsigned ii = i + (k & 1), jj = j + (k >> 1);
            const float lon = 2.0f * (float)M_PI * ii / su, lat = (float)M_PI * jj / sv - (float)M_PI / 2;
            q[k][0] = cosf(lat) * sinf(lon);
            q[k][1] = sinf(lat);
            q[k][2] = cosf(lat) * cosf(lon);
         }
         static const int o[6] = { 0, 1, 2, 1, 3, 2 };
         for (int k = 0; k < 6; k++) {
            memcpy(v, q[o[k]], 12); /* position */
            memcpy(v + 3, q[o[k]], 12); /* normal (a unit sphere) */
            v += 6;
         }
      }
   for (int f = 0; f < 2; f++) {
      step("litmesh frame %d (%u triangles)", f + 1, ntri);
      VkCommandBuffer cmd = begin(0);
      begin_pass(cmd, rp, fb, w, h, CLEAR, 1);
      vkCmdBindPipeline(cmd, VK_PIPELINE_BIND_POINT_GRAPHICS, p);
      const VkDeviceSize off = 0;
      vkCmdBindVertexBuffers(cmd, 0, 1, &vb.b, &off);
      vkCmdDraw(cmd, 3 * ntri, 1, 0, 0);
      vkCmdEndRenderPass(cmd);
      readback(cmd, &t, &rb[f]);
      end_submit(cmd);
   }
   if (nocheck) {
      printf("RESULT %s PASS (not checked) %u triangles, %.1f ms\n", rung, ntri, last_ms);
      finish(0);
   }
   const uint8_t *a = rb[0].p, *b = rb[1].p;
   uint8_t clear[4];
   bgra(clear, CLEAR);
   size_t diff = 0, in_bad = 0, out_bad = 0, in_n = 0, out_n = 0;
   const float R = 0.8f * 128.0f;
   for (uint32_t y = 0; y < h; y++)
      for (uint32_t x = 0; x < w; x++) {
         const size_t k = 4 * ((size_t)y * w + x);
         diff += memcmp(a + k, b + k, 4) != 0;
         const float r = hypotf(x + 0.5f - 128.0f, y + 0.5f - 128.0f);
         const int is_clear = !memcmp(a + k, clear, 4);
         if (r < R - 2) {
            in_n++;
            in_bad += is_clear || a[k + 3] != 255 || a[k + 2] < 36 || a[k + 2] > 245;
         } else if (r > R + 2) {
            out_n++;
            out_bad += !is_clear;
         }
      }
   if (!diff && !in_bad && !out_bad) {
      printf("RESULT %s PASS %u triangles, lit %zu/%zu inside, clear %zu/%zu outside, frames identical, %.1f ms\n",
             rung, ntri, in_n, in_n, out_n, out_n, last_ms);
      finish(0);
   }
   printf("RESULT %s FAIL %u triangles: inside wrong %zu/%zu, outside wrong %zu/%zu, frames differ in %zu pixels\n",
          rung, ntri, in_bad, in_n, out_bad, out_n, diff);
   finish(1);
}

static void r_rtt(void)
{
   /* pass 1: the draws16 grid into a sampled 64x64 target */
   struct rect s = rect_setup(64, 64, SHADER(l_pc_frag), 0, 0, 0, VK_IMAGE_USAGE_SAMPLED_BIT,
                              VK_IMAGE_LAYOUT_SHADER_READ_ONLY_OPTIMAL);
   VkCommandBuffer cmd = begin(0);
   rect_begin(cmd, &s, CLEAR);
   grid_draws(cmd, s.l);
   vkCmdEndRenderPass(cmd);
   end_submit(cmd);
   const double first = last_ms;
   /* pass 2: sample it, nearest, into another 64x64 target */
   uint8_t *got = sample_pass(&s.t, VK_FILTER_NEAREST, 64, 64);
   struct expect e = newexpect(64, 64, CLEAR);
   grid_expect(&e);
   char d[96];
   snprintf(d, sizeof(d), "grid rendered (%.1f ms) then sampled (%.1f ms)", first, last_ms);
   check(&e, got, 1, d);
}

static void compute_setup(const uint32_t *spv, size_t size, VkDescriptorSet *set, VkPipelineLayout *pl,
                          VkPipeline *pipe, const struct buf *b, VkDeviceSize range)
{
   VkDescriptorSetLayout dsl = newdsl(VK_DESCRIPTOR_TYPE_STORAGE_BUFFER, VK_SHADER_STAGE_COMPUTE_BIT);
   const VkPipelineLayoutCreateInfo li = { .sType = VK_STRUCTURE_TYPE_PIPELINE_LAYOUT_CREATE_INFO,
                                           .setLayoutCount = 1, .pSetLayouts = &dsl };
   CK(vkCreatePipelineLayout(dev, &li, NULL, pl));
   const VkComputePipelineCreateInfo ci = {
      .sType = VK_STRUCTURE_TYPE_COMPUTE_PIPELINE_CREATE_INFO,
      .stage = { .sType = VK_STRUCTURE_TYPE_PIPELINE_SHADER_STAGE_CREATE_INFO, .stage = VK_SHADER_STAGE_COMPUTE_BIT,
                 .module = shader(spv, size), .pName = "main" },
      .layout = *pl };
   step("vkCreateComputePipelines");
   CK(vkCreateComputePipelines(dev, VK_NULL_HANDLE, 1, &ci, NULL, pipe));
   const VkDescriptorBufferInfo bi = { b->b, 0, range };
   *set = newset(dsl, VK_DESCRIPTOR_TYPE_STORAGE_BUFFER, &bi, NULL);
}

static void r_compute64k(void)
{
   const uint32_t n = 65536;
   struct buf b = newbuf(n * 4 + 4096, VK_BUFFER_USAGE_STORAGE_BUFFER_BIT);
   memset(b.p, 0xee, n * 4 + 4096);
   VkDescriptorSet set;
   VkPipelineLayout pl;
   VkPipeline p;
   compute_setup(l_big_comp, sizeof(l_big_comp), &set, &pl, &p, &b, n * 4);
   VkCommandBuffer cmd = begin(0);
   vkCmdBindPipeline(cmd, VK_PIPELINE_BIND_POINT_COMPUTE, p);
   vkCmdBindDescriptorSets(cmd, VK_PIPELINE_BIND_POINT_COMPUTE, pl, 0, 1, &set, 0, NULL);
   vkCmdDispatch(cmd, n / 32, 1, 1);
   barrier(cmd, VK_PIPELINE_STAGE_COMPUTE_SHADER_BIT, VK_ACCESS_SHADER_WRITE_BIT, VK_PIPELINE_STAGE_HOST_BIT,
           VK_ACCESS_HOST_READ_BIT);
   end_submit(cmd);
   if (nocheck) {
      printf("RESULT %s PASS (not checked) job %.1f ms\n", rung, last_ms);
      finish(0);
   }
   const uint32_t *o = b.p;
   uint32_t bad = 0, first = 0;
   for (uint32_t i = 0; i < n; i++)
      if (o[i] != i && !bad++)
         first = i;
   const uint32_t guard = o[n];
   if (!bad && guard == 0xeeeeeeeeu) {
      printf("RESULT %s PASS 65536/65536 values (2048 workgroups of 32) job %.1f ms\n", rung, last_ms);
      finish(0);
   }
   printf("RESULT %s FAIL %u/65536 wrong, first out[%u] = %#x, guard %#x, job %.1f ms\n", rung, bad, first,
          bad ? o[first] : 0, guard, last_ms);
   finish(1);
}

static void r_c2g(void)
{
   struct tri s = tri_setup(64, 64);
   struct buf vb = newbuf(256, VK_BUFFER_USAGE_STORAGE_BUFFER_BIT | VK_BUFFER_USAGE_VERTEX_BUFFER_BIT);
   VkDescriptorSet set;
   VkPipelineLayout pl;
   VkPipeline p;
   compute_setup(l_verts_comp, sizeof(l_verts_comp), &set, &pl, &p, &vb, 48);
   VkCommandBuffer cmd = begin(0);
   vkCmdBindPipeline(cmd, VK_PIPELINE_BIND_POINT_COMPUTE, p);
   vkCmdBindDescriptorSets(cmd, VK_PIPELINE_BIND_POINT_COMPUTE, pl, 0, 1, &set, 0, NULL);
   vkCmdDispatch(cmd, 1, 1, 1);
   barrier(cmd, VK_PIPELINE_STAGE_COMPUTE_SHADER_BIT, VK_ACCESS_SHADER_WRITE_BIT, VK_PIPELINE_STAGE_VERTEX_INPUT_BIT,
           VK_ACCESS_VERTEX_ATTRIBUTE_READ_BIT);
   tri_record(cmd, &s, vb.b);
   end_submit(cmd);
   struct expect e = newexpect(64, 64, CLEAR);
   expect_tri(&e, TRI);
   check(&e, s.rb.p, 1, "vertices written by a compute dispatch in the same command buffer");
}

static void r_timing(void)
{
   uint32_t nq = 1;
   VkQueueFamilyProperties qf;
   vkGetPhysicalDeviceQueueFamilyProperties(phy, &nq, &qf);
   VkPhysicalDeviceProperties props;
   vkGetPhysicalDeviceProperties(phy, &props);
   /* wall-clock per job: 10 clears, 10 triangles, 10 small dispatches */
   double c_ms[3] = { 0 }, c_max[3] = { 0 };
   {
      struct img t = newtarget(64, 64, 0);
      VkRenderPass rp = newrp(VK_FORMAT_UNDEFINED, VK_IMAGE_LAYOUT_TRANSFER_SRC_OPTIMAL);
      VkFramebuffer fb = newfb(rp, &t, NULL);
      VkCommandBuffer cmd = begin(1);
      begin_pass(cmd, rp, fb, 64, 64, CLEAR, 0);
      vkCmdEndRenderPass(cmd);
      CK(vkEndCommandBuffer(cmd));
      for (int i = 0; i < 10; i++) {
         submit(cmd);
         c_ms[0] += last_ms / 10;
         c_max[0] = fmax(c_max[0], last_ms);
      }
   }
   {
      struct tri s = tri_setup(64, 64);
      VkCommandBuffer cmd = begin(1);
      tri_record(cmd, &s, s.vb.b);
      CK(vkEndCommandBuffer(cmd));
      for (int i = 0; i < 10; i++) {
         submit(cmd);
         c_ms[1] += last_ms / 10;
         c_max[1] = fmax(c_max[1], last_ms);
      }
   }
   {
      struct buf b = newbuf(4096, VK_BUFFER_USAGE_STORAGE_BUFFER_BIT);
      VkDescriptorSet set;
      VkPipelineLayout pl;
      VkPipeline p;
      compute_setup(l_big_comp, sizeof(l_big_comp), &set, &pl, &p, &b, 4096);
      VkCommandBuffer cmd = begin(1);
      vkCmdBindPipeline(cmd, VK_PIPELINE_BIND_POINT_COMPUTE, p);
      vkCmdBindDescriptorSets(cmd, VK_PIPELINE_BIND_POINT_COMPUTE, pl, 0, 1, &set, 0, NULL);
      vkCmdDispatch(cmd, 1, 1, 1);
      CK(vkEndCommandBuffer(cmd));
      for (int i = 0; i < 10; i++) {
         submit(cmd);
         c_ms[2] += last_ms / 10;
         c_max[2] = fmax(c_max[2], last_ms);
      }
   }
   printf("RESULT %s PASS timestampValidBits %u timestampPeriod %.1f ns (queries not used: wall clock per job) "
          "clear64 avg %.2f max %.2f ms, tri64 avg %.2f max %.2f ms, compute32 avg %.2f max %.2f ms\n",
          rung, qf.timestampValidBits, props.limits.timestampPeriod, c_ms[0], c_max[0], c_ms[1], c_max[1], c_ms[2],
          c_max[2]);
   finish(0);
}

/* ---- round 3: vkCmdClearColorImage on the GPU, vkmark's own shaders against a CPU reference ---- */

/* level `level` of t (TRANSFER_SRC_OPTIMAL) into rb */
static void readback_level(VkCommandBuffer cmd, const struct img *t, uint32_t level, const struct buf *rb)
{
   const VkBufferImageCopy c = { .imageSubresource = { VK_IMAGE_ASPECT_COLOR_BIT, level, 0, 1 },
                                 .imageExtent = { MAX2(t->w >> level, 1u), MAX2(t->h >> level, 1u), 1 } };
   vkCmdCopyImageToBuffer(cmd, t->i, VK_IMAGE_LAYOUT_TRANSFER_SRC_OPTIMAL, rb->b, 1, &c);
   barrier(cmd, VK_PIPELINE_STAGE_TRANSFER_BIT, VK_ACCESS_TRANSFER_WRITE_BIT, VK_PIPELINE_STAGE_HOST_BIT,
           VK_ACCESS_HOST_READ_BIT);
}

/* clearimg: vkCmdClearColorImage (vkmark's clear scene) to colour A, then again to colour B, in one command buffer;
 * every pixel checked. mode 0: level 0 of the two-level twiddled target (the target shape the A8 has drawn);
 * mode 1: the first clear covers levels 0 and 1 (a render into mip level 1, untested on the A8), level 1 must be A;
 * mode 2: a LINEAR-tiling target (Apple's passcap `linear` encoding; optimal one-level images are twiddled on the
 * A8, as Metal's). The job times are the
 * fence waits (on the A8 a render per clear and level). */
static void r_clearimg(uint32_t w, uint32_t h, int mode)
{
   struct img t = mode == 2 ? newimg_ex(w, h, VK_FORMAT_B8G8R8A8_UNORM, VK_IMAGE_USAGE_COLOR_ATTACHMENT_BIT |
                                        VK_IMAGE_USAGE_TRANSFER_SRC_BIT | VK_IMAGE_USAGE_TRANSFER_DST_BIT, 1, 1, 1)
                            : newtarget(w, h, VK_IMAGE_USAGE_TRANSFER_DST_BIT);
   struct buf rb0 = newbuf((VkDeviceSize)w * h * 4, VK_BUFFER_USAGE_TRANSFER_DST_BIT);
   struct buf rb1 = newbuf((VkDeviceSize)(w / 2) * (h / 2) * 4, VK_BUFFER_USAGE_TRANSFER_DST_BIT);
   const float ca[4] = { 0.75f, 0.25f, 0.5f, 1.0f }, cb[4] = { 0.25f, 0.5f, 0.75f, 0.5f };
   VkClearColorValue va, vb;
   memcpy(va.float32, ca, 16);
   memcpy(vb.float32, cb, 16);
   const VkImageSubresourceRange both = { VK_IMAGE_ASPECT_COLOR_BIT, 0, 2, 0, 1 };
   const VkImageSubresourceRange l0 = { VK_IMAGE_ASPECT_COLOR_BIT, 0, 1, 0, 1 };
   VkCommandBuffer cmd = begin(0);
   image_barrier(cmd, t.i, VK_IMAGE_LAYOUT_UNDEFINED, VK_IMAGE_LAYOUT_TRANSFER_DST_OPTIMAL,
                 VK_PIPELINE_STAGE_TOP_OF_PIPE_BIT, 0, VK_PIPELINE_STAGE_TRANSFER_BIT, VK_ACCESS_TRANSFER_WRITE_BIT);
   vkCmdClearColorImage(cmd, t.i, VK_IMAGE_LAYOUT_TRANSFER_DST_OPTIMAL, &va, 1, mode == 1 ? &both : &l0);
   barrier(cmd, VK_PIPELINE_STAGE_TRANSFER_BIT, VK_ACCESS_TRANSFER_WRITE_BIT, VK_PIPELINE_STAGE_TRANSFER_BIT,
           VK_ACCESS_TRANSFER_WRITE_BIT);
   vkCmdClearColorImage(cmd, t.i, VK_IMAGE_LAYOUT_TRANSFER_DST_OPTIMAL, &vb, 1, &l0);
   image_barrier(cmd, t.i, VK_IMAGE_LAYOUT_TRANSFER_DST_OPTIMAL, VK_IMAGE_LAYOUT_TRANSFER_SRC_OPTIMAL,
                 VK_PIPELINE_STAGE_TRANSFER_BIT, VK_ACCESS_TRANSFER_WRITE_BIT, VK_PIPELINE_STAGE_TRANSFER_BIT,
                 VK_ACCESS_TRANSFER_READ_BIT);
   readback_level(cmd, &t, 0, &rb0);
   if (mode == 1)
      readback_level(cmd, &t, 1, &rb1);
   end_submit(cmd);
   const double clear_ms = last_ms;
   /* the same command buffer shape once more: the second run is the steady-state time (no first-use setup) */
   VkCommandBuffer cmd2 = begin(0);
   image_barrier(cmd2, t.i, VK_IMAGE_LAYOUT_TRANSFER_SRC_OPTIMAL, VK_IMAGE_LAYOUT_TRANSFER_DST_OPTIMAL,
                 VK_PIPELINE_STAGE_TRANSFER_BIT, 0, VK_PIPELINE_STAGE_TRANSFER_BIT, VK_ACCESS_TRANSFER_WRITE_BIT);
   vkCmdClearColorImage(cmd2, t.i, VK_IMAGE_LAYOUT_TRANSFER_DST_OPTIMAL, &vb, 1, &l0);
   image_barrier(cmd2, t.i, VK_IMAGE_LAYOUT_TRANSFER_DST_OPTIMAL, VK_IMAGE_LAYOUT_TRANSFER_SRC_OPTIMAL,
                 VK_PIPELINE_STAGE_TRANSFER_BIT, VK_ACCESS_TRANSFER_WRITE_BIT, VK_PIPELINE_STAGE_TRANSFER_BIT,
                 VK_ACCESS_TRANSFER_READ_BIT);
   end_submit(cmd2);
   char d[128];
   snprintf(d, sizeof(d), "%s; first job %.2f ms (2%s clears + copy), one clear %.2f ms",
            mode == 1 ? "level 0 = B, level 1 = A" : mode == 2 ? "one-level target = B" : "level 0 = B", clear_ms,
            mode == 1 ? "+1" : "", last_ms);
   if (mode == 1 && !nocheck) {
      struct expect e1 = newexpect(w / 2, h / 2, ca);
      size_t bad = 0;
      for (size_t i = 0; i < (size_t)(w / 2) * (h / 2); i++)
         for (int c = 0; c < 4; c++)
            bad += abs((int)((uint8_t *)rb1.p)[4 * i + c] - (int)e1.px[4 * i + c]) > 1;
      if (bad) {
         const uint8_t *g = rb1.p;
         printf("RESULT %s FAIL level 1 (%ux%u) not colour A: %zu bytes off, first texel %02x%02x%02x%02x want "
                "%02x%02x%02x%02x\n", rung, w / 2, h / 2, bad, g[0], g[1], g[2], g[3], e1.px[0], e1.px[1], e1.px[2],
                e1.px[3]);
         finish(1);
      }
   }
   struct expect e0 = newexpect(w, h, cb);
   check(&e0, rb0.p, 1, d);
}

/* vkmark's SPIR-V, read from the vkmark side bundle (A8_VKMARK_SHADERS, default <bin>/../vkmark/data/shaders):
 * exactly the shaders vkmark runs, not copies. A missing bundle is this rung's SKIP. */
static VkShaderModule vkm_shader(const char *name)
{
   char path[512], exe[400];
   const char *dir = getenv("A8_VKMARK_SHADERS");
   if (dir) {
      snprintf(path, sizeof(path), "%s/%s.spv", dir, name);
   } else {
      const ssize_t n = readlink("/proc/self/exe", exe, sizeof(exe) - 1);
      exe[n > 0 ? n : 0] = 0;
      char *slash = strrchr(exe, '/');
      if (slash)
         *slash = 0;
      snprintf(path, sizeof(path), "%s/../vkmark/data/shaders/%s.spv", exe, name);
   }
   FILE *f = fopen(path, "rb");
   if (!f) {
      printf("RESULT %s SKIP vkmark's shader %s not found (install the vkmark side bundle next to the ladder, or set "
             "A8_VKMARK_SHADERS)\n", rung, path);
      finish(0);
   }
   fseek(f, 0, SEEK_END);
   const long size = ftell(f);
   fseek(f, 0, SEEK_SET);
   uint32_t *code = malloc(size);
   if (fread(code, 1, size, f) != (size_t)size) {
      printf("RESULT %s FAIL short read of %s\n", rung, path);
      finish(1);
   }
   fclose(f);
   step("vkmark shader %s (%ld bytes)", path, size);
   return shader(code, size);
}

/* a 256x256 RGBA8 texture of hashed texels (memory order R,G,B,A): any wrong texel, tap or weight shows */
static uint8_t vkm_hash(int x, int y, int c)
{
   uint32_t v = (uint32_t)x * 73856093u ^ (uint32_t)y * 19349663u ^ (uint32_t)(c + 1) * 83492791u;
   v ^= v >> 13;
   v *= 0x5bd1e995u;
   v ^= v >> 15;
   return (uint8_t)v;
}

static uint8_t vkm_tex_val(int x, int y, int c, int l)
{
   (void)l;
   return vkm_hash(x, y, c);
}

/* set 0: binding 0 the uniform block, binding 1 the combined image sampler (vkmark's layout for all three scenes) */
static VkDescriptorSetLayout vkm_dsl(void)
{
   const VkDescriptorSetLayoutBinding b[2] = {
      { 0, VK_DESCRIPTOR_TYPE_UNIFORM_BUFFER, 1, VK_SHADER_STAGE_VERTEX_BIT | VK_SHADER_STAGE_FRAGMENT_BIT },
      { 1, VK_DESCRIPTOR_TYPE_COMBINED_IMAGE_SAMPLER, 1, VK_SHADER_STAGE_FRAGMENT_BIT },
   };
   const VkDescriptorSetLayoutCreateInfo li = { .sType = VK_STRUCTURE_TYPE_DESCRIPTOR_SET_LAYOUT_CREATE_INFO,
                                                .bindingCount = 2, .pBindings = b };
   VkDescriptorSetLayout l;
   CK(vkCreateDescriptorSetLayout(dev, &li, NULL, &l));
   return l;
}

static VkDescriptorSet vkm_set(VkDescriptorSetLayout dsl, const struct buf *ubo, VkDeviceSize ubo_size,
                               const struct img *tex, VkSampler smp)
{
   const VkDescriptorPoolSize ps[2] = { { VK_DESCRIPTOR_TYPE_UNIFORM_BUFFER, 1 },
                                        { VK_DESCRIPTOR_TYPE_COMBINED_IMAGE_SAMPLER, 1 } };
   const VkDescriptorPoolCreateInfo pi = { .sType = VK_STRUCTURE_TYPE_DESCRIPTOR_POOL_CREATE_INFO, .maxSets = 1,
                                           .poolSizeCount = 2, .pPoolSizes = ps };
   VkDescriptorPool dp;
   CK(vkCreateDescriptorPool(dev, &pi, NULL, &dp));
   const VkDescriptorSetAllocateInfo ai = { .sType = VK_STRUCTURE_TYPE_DESCRIPTOR_SET_ALLOCATE_INFO,
                                            .descriptorPool = dp, .descriptorSetCount = 1, .pSetLayouts = &dsl };
   VkDescriptorSet s;
   CK(vkAllocateDescriptorSets(dev, &ai, &s));
   const VkDescriptorBufferInfo bi = { ubo->b, 0, ubo_size };
   const VkDescriptorImageInfo ii = { smp, tex->v, VK_IMAGE_LAYOUT_SHADER_READ_ONLY_OPTIMAL };
   const VkWriteDescriptorSet w[2] = {
      { .sType = VK_STRUCTURE_TYPE_WRITE_DESCRIPTOR_SET, .dstSet = s, .dstBinding = 0, .descriptorCount = 1,
        .descriptorType = VK_DESCRIPTOR_TYPE_UNIFORM_BUFFER, .pBufferInfo = &bi },
      { .sType = VK_STRUCTURE_TYPE_WRITE_DESCRIPTOR_SET, .dstSet = s, .dstBinding = 1, .descriptorCount = 1,
        .descriptorType = VK_DESCRIPTOR_TYPE_COMBINED_IMAGE_SAMPLER, .pImageInfo = &ii },
   };
   vkUpdateDescriptorSets(dev, 2, w, 0, NULL);
   return s;
}

/* column-major mat4 (std140): 0.5^k scale s, rotation by 90 degrees (x,y) -> (-y,x) when rot, translation (tx,ty) */
static void vkm_mat(float *m, float s, int rot, float tx, float ty)
{
   memset(m, 0, 64);
   if (rot) {
      m[1] = s;  /* column 0 = (0, s) */
      m[4] = -s; /* column 1 = (-s, 0) */
   } else {
      m[0] = s;
      m[5] = s;
   }
   m[10] = 1;
   m[12] = tx;
   m[13] = ty;
   m[15] = 1;
}

/* "vkmark texture/effect2d/desktop shaders at 256x256": which scene; every pixel against the CPU */
enum vkm_scene { VKM_TEXTURE, VKM_BLUR, VKM_DESKTOP };

static void r_vkmark(enum vkm_scene scene)
{
   const uint32_t W = 256, H = 256;
   struct img tex = upload_tex(256, 256, VK_FORMAT_R8G8B8A8_UNORM, 2, 1, vkm_tex_val);
   VkSampler smp = newsampler_ex(VK_FILTER_NEAREST, VK_SAMPLER_MIPMAP_MODE_NEAREST,
                                 VK_SAMPLER_ADDRESS_MODE_CLAMP_TO_EDGE, 0);
   VkDescriptorSetLayout dsl = vkm_dsl();
   /* vkmark's headless image: one level, optimal (twiddled on the A8) */
   struct img t = newimg(W, H, VK_FORMAT_B8G8R8A8_UNORM, VK_IMAGE_USAGE_COLOR_ATTACHMENT_BIT |
                         VK_IMAGE_USAGE_TRANSFER_SRC_BIT, 1);
   VkRenderPass rp = newrp(VK_FORMAT_UNDEFINED, VK_IMAGE_LAYOUT_TRANSFER_SRC_OPTIMAL);
   VkFramebuffer fb = newfb(rp, &t, NULL);
   struct buf rb = newbuf((VkDeviceSize)W * H * 4, VK_BUFFER_USAGE_TRANSFER_DST_BIT);
   VkPipelineLayout pl = newlayout(dsl);
   const char *vsn = scene == VKM_TEXTURE ? "light-basic-tex.vert" : scene == VKM_BLUR ? "effect2d.vert" : "desktop.vert";
   const char *fsn = scene == VKM_TEXTURE ? "light-basic-tex.frag" : scene == VKM_BLUR ? "effect2d-blur.frag" : "desktop.frag";
   const struct pipe_cfg pc = { vkm_shader(vsn), vkm_shader(fsn), scene == VKM_TEXTURE ? VIN_POS3_NRM3_TC2 : VIN_POS2_TC2,
                                0, 0, pl, rp, W, H };
   VkPipeline p = newpipe(&pc);

   /* the full-screen quad (two triangles), and for desktop a window quad with texcoords 0..0.5 */
   static const float qp[6][2] = { { -1, -1 }, { 1, -1 }, { -1, 1 }, { -1, 1 }, { 1, -1 }, { 1, 1 } };
   struct buf vb = newbuf(2 * 6 * 32, VK_BUFFER_USAGE_VERTEX_BUFFER_BIT);
   float *v = vb.p;
   /* texture: normal (1,-1,2) (the NormalMatrix turns it to (1,1,2): diffuse 0.272 instead of 0.816) */
   for (int i = 0; i < 6; i++) {
      if (scene == VKM_TEXTURE) {
         const float d[8] = { qp[i][0], qp[i][1], 0, 1, -1, 2, (qp[i][0] + 1) / 2, (qp[i][1] + 1) / 2 };
         memcpy(v + 8 * i, d, 32);
      } else {
         const float d[4] = { qp[i][0], qp[i][1], (qp[i][0] + 1) / 2, (qp[i][1] + 1) / 2 };
         const float w[4] = { qp[i][0], qp[i][1], (qp[i][0] + 1) / 4, (qp[i][1] + 1) / 4 };
         memcpy(v + 4 * i, d, 16);
         memcpy(v + 24 + 4 * i, w, 16);
      }
   }
   /* uniform blocks: texture {MVP = rotate 90, NormalMatrix = rotate 90, MaterialDiffuse}; blur {1/256, 1/256};
    * desktop {identity} for the background and {0.5 * rotate 90 + (0.25, -0.25)} for the window */
   const float diffuse[4] = { 1.0f, 0.8f, 0.6f, 0.9f };
   struct buf u0 = newbuf(256, VK_BUFFER_USAGE_UNIFORM_BUFFER_BIT), u1 = newbuf(256, VK_BUFFER_USAGE_UNIFORM_BUFFER_BIT);
   float *uf = u0.p;
   if (scene == VKM_TEXTURE) {
      vkm_mat(uf, 1, 1, 0, 0);
      vkm_mat(uf + 16, 1, 1, 0, 0);
      memcpy(uf + 32, diffuse, 16);
   } else if (scene == VKM_BLUR) {
      uf[0] = 1.0f / W;
      uf[1] = 1.0f / H;
   } else {
      vkm_mat(uf, 1, 0, 0, 0);
      vkm_mat(u1.p, 0.5f, 1, 0.25f, -0.25f);
   }
   VkDescriptorSet s0 = vkm_set(dsl, &u0, scene == VKM_TEXTURE ? 144 : scene == VKM_BLUR ? 16 : 64, &tex, smp);
   VkDescriptorSet s1 = scene == VKM_DESKTOP ? vkm_set(dsl, &u1, 64, &tex, smp) : VK_NULL_HANDLE;

   const float clear[4] = { 0.0f, 0.0f, 0.0f, 0.0f };
   VkCommandBuffer cmd = begin(0);
   begin_pass(cmd, rp, fb, W, H, clear, 0);
   vkCmdBindPipeline(cmd, VK_PIPELINE_BIND_POINT_GRAPHICS, p);
   const VkDeviceSize off = 0, off2 = 6 * 16;
   vkCmdBindVertexBuffers(cmd, 0, 1, &vb.b, &off);
   vkCmdBindDescriptorSets(cmd, VK_PIPELINE_BIND_POINT_GRAPHICS, pl, 0, 1, &s0, 0, NULL);
   vkCmdDraw(cmd, 6, 1, 0, 0);
   if (scene == VKM_DESKTOP) {
      vkCmdBindVertexBuffers(cmd, 0, 1, &vb.b, &off2);
      vkCmdBindDescriptorSets(cmd, VK_PIPELINE_BIND_POINT_GRAPHICS, pl, 0, 1, &s1, 0, NULL);
      vkCmdDraw(cmd, 6, 1, 0, 0);
   }
   vkCmdEndRenderPass(cmd);
   readback(cmd, &t, &rb);
   end_submit(cmd);

   /* the CPU reference (target bytes B,G,R,A; texture bytes R,G,B,A) */
   struct expect e = newexpect(W, H, clear);
   const double ln = sqrt(20.0 * 20 + 20 * 20 + 10 * 10), nl = sqrt(6.0);
   const double dif = fmax((1 * 20.0 / ln + 1 * -20.0 / ln + 2 * 10.0 / ln) / nl, 0); /* N = (1,1,2)/sqrt 6 */
   size_t window = 0;
   for (uint32_t y = 0; y < H; y++)
      for (uint32_t x = 0; x < W; x++) {
         const double nx = (x + 0.5) / W * 2 - 1, ny = (y + 0.5) / H * 2 - 1;
         uint8_t out[4]; /* R,G,B,A */
         if (scene == VKM_TEXTURE) {
            /* MVP rotates by 90: pixel (nx, ny) shows the quad point (ny, -nx) */
            const int u = (int)floor((ny + 1) / 2 * 256), w = (int)floor((-nx + 1) / 2 * 256);
            for (int c = 0; c < 4; c++) {
               const double col = c < 3 ? dif * diffuse[c] : diffuse[3];
               out[c] = (uint8_t)lround(vkm_hash(u, w, c) * col);
            }
         } else if (scene == VKM_BLUR) {
            for (int c = 0; c < 3; c++) {
               double acc = 0;
               for (int j = -1; j <= 1; j++)
                  for (int i = -2; i <= 2; i++) {
                     const int tx = (int)x + i < 0 ? 0 : (int)x + i > 255 ? 255 : (int)x + i;
                     const int ty = (int)y + j < 0 ? 0 : (int)y + j > 255 ? 255 : (int)y + j;
                     acc += vkm_hash(tx, ty, c) * 0.066667;
                  }
               out[c] = (uint8_t)fmin(lround(acc), 255);
            }
            out[3] = 255;
         } else {
            /* the window: quad point (x', y') = ((ny + 0.25) * 2, -(nx - 0.25) * 2), texcoord (p + 1) / 4 */
            const double px = (ny + 0.25) * 2, py = -(nx - 0.25) * 2;
            const double edge = 2.0 / 128; /* one window pixel */
            int u = (int)x, w = (int)y;
            if (fabs(px) < 1 && fabs(py) < 1) {
               if (fabs(px) > 1 - edge || fabs(py) > 1 - edge) {
                  e.skip[(size_t)y * W + x] = 1;
                  continue;
               }
               u = (int)floor((px + 1) / 4 * 256);
               w = (int)floor((py + 1) / 4 * 256);
               window++;
            }
            for (int c = 0; c < 4; c++)
               out[c] = vkm_hash(u, w, c);
         }
         const uint8_t b[4] = { out[2], out[1], out[0], out[3] };
         expect_bytes(&e, x, y, b);
      }
   char d[160];
   snprintf(d, sizeof(d), "vkmark's %s + %s, %s, job %.2f ms", vsn, fsn,
            scene == VKM_TEXTURE ? "texel * lit colour (MVP and NormalMatrix rotate 90)"
            : scene == VKM_BLUR  ? "15-tap blur of a hashed texture"
                                 : "background + rotated half-size window", last_ms);
   if (scene == VKM_DESKTOP && !nocheck && window < 120 * 120) {
      printf("RESULT %s FAIL reference bug: window covers %zu pixels\n", rung, window);
      finish(1);
   }
   check(&e, rb.p, 2, d);
}

/* texminperf: what one-level texture layouts cost the GPU when sampled minified (vkmark's effect2d: its 800x600
 * background, one level, drawn into 256x256 with a 15-tap blur). vkmark's own effect2d SPIR-V, LINEAR, the quad drawn
 * 8 times a pass, the pass submitted 10 times; per layout the average job time, and the first pass checked against a
 * CPU bilinear reference, every pixel. Layouts: an optimal 800x600 one-level image (twiddled on the A8 since update
 * 29, padded to 1024x1024), the same texels in a LINEAR-tiling image (linear, Apple's buffer-backed form), and an
 * optimal 1024x1024 one-level image (twiddled, no padding), sampled over the same 800x600 texel window. */
static uint8_t tmp_val(int x, int y, int c)
{
   return vkm_hash(x, y, c);
}

static double bilerp(const uint8_t *tx, int tw, int th, int pitch, double u, double v, int c)
{
   /* u, v in texels; texel centres at +0.5; clamp to edge */
   const double fx = u - 0.5, fy = v - 0.5;
   const int x0 = (int)floor(fx), y0 = (int)floor(fy);
   const double ax = fx - x0, ay = fy - y0;
   double acc = 0;
   for (int j = 0; j < 2; j++)
      for (int i = 0; i < 2; i++) {
         int x = x0 + i, y = y0 + j;
         x = x < 0 ? 0 : x > tw - 1 ? tw - 1 : x;
         y = y < 0 ? 0 : y > th - 1 ? th - 1 : y;
         acc += tx[y * pitch + 4 * x + c] * (i ? ax : 1 - ax) * (j ? ay : 1 - ay);
      }
   return acc;
}

static struct img texminperf_first;

static void r_texminperf(void)
{
   const uint32_t W = 256, H = 256, TW = 800, TH = 600;
   /* two samplers: maxLod 0 (no mip selection possible), and a trilinear one with maxLod VK_LOD_CLAMP_NONE (an
    * application that does not know the texture has one level: the TPU must still read one level) */
   VkSampler smps[2] = { newsampler_ex(VK_FILTER_LINEAR, VK_SAMPLER_MIPMAP_MODE_NEAREST,
                                       VK_SAMPLER_ADDRESS_MODE_CLAMP_TO_EDGE, 0),
                         newsampler_ex(VK_FILTER_LINEAR, VK_SAMPLER_MIPMAP_MODE_LINEAR,
                                       VK_SAMPLER_ADDRESS_MODE_CLAMP_TO_EDGE, VK_LOD_CLAMP_NONE) };
   VkDescriptorSetLayout dsl = vkm_dsl();
   struct img t = newimg_ex(W, H, VK_FORMAT_B8G8R8A8_UNORM, VK_IMAGE_USAGE_COLOR_ATTACHMENT_BIT |
                            VK_IMAGE_USAGE_TRANSFER_SRC_BIT, 1, 1, 0);
   VkRenderPass rp = newrp(VK_FORMAT_UNDEFINED, VK_IMAGE_LAYOUT_TRANSFER_SRC_OPTIMAL);
   VkFramebuffer fb = newfb(rp, &t, NULL);
   struct buf rb = newbuf((VkDeviceSize)W * H * 4, VK_BUFFER_USAGE_TRANSFER_DST_BIT);
   VkPipelineLayout pl = newlayout(dsl);
   const struct pipe_cfg pc = { vkm_shader("effect2d.vert"), vkm_shader("effect2d-blur.frag"), VIN_POS2_TC2, 0, 0, pl, rp,
                                W, H };
   VkPipeline p = newpipe(&pc);
   static const float qp[6][2] = { { -1, -1 }, { 1, -1 }, { -1, 1 }, { -1, 1 }, { 1, -1 }, { 1, 1 } };
   const char *names[3] = { "optimal 800x600 (twiddled, padded)", "LINEAR-tiling 800x600 (linear)",
                            "optimal 1024x1024 (twiddled), 800x600 window" };
   double avg[3][3] = { { 0 } };
   uint8_t *src = malloc(1024 * 1024 * 4);
   for (int y = 0; y < 1024; y++)
      for (int x = 0; x < 1024; x++)
         for (int c = 0; c < 4; c++)
            src[4 * (y * 1024 + x) + c] = tmp_val(x, y, c);
   /* sm 2: vkmark's own texture format, R8G8B8A8_SRGB (its effect2d/desktop/texture images), trilinear sampler;
    * timed only (no sRGB reference) */
   for (int vs = 0; vs < 9; vs++) {
      const int v = vs / 3, sm = vs % 3;
      VkSampler smp = smps[sm ? 1 : 0];
      const VkFormat tf = sm == 2 ? VK_FORMAT_R8G8B8A8_SRGB : VK_FORMAT_R8G8B8A8_UNORM;
      const uint32_t iw = v == 2 ? 1024 : TW, ih = v == 2 ? 1024 : TH;
      static struct img texs[3];
      struct img tex;
      if (sm == 1) {
         tex = texs[v];
      } else if (v == 1) {
         tex = newimg_ex(iw, ih, tf, VK_IMAGE_USAGE_SAMPLED_BIT, 1, 1, 1);
         const VkImageSubresource sub = { VK_IMAGE_ASPECT_COLOR_BIT, 0, 0 };
         VkSubresourceLayout lay;
         vkGetImageSubresourceLayout(dev, tex.i, &sub, &lay);
         uint8_t *map;
         CK(vkMapMemory(dev, tex.m, 0, VK_WHOLE_SIZE, 0, (void **)&map));
         for (uint32_t y = 0; y < ih; y++)
            memcpy(map + lay.offset + y * lay.rowPitch, src + 4 * y * 1024, 4 * iw);
         VkCommandBuffer cmd = begin(0);
         image_barrier(cmd, tex.i, VK_IMAGE_LAYOUT_PREINITIALIZED, VK_IMAGE_LAYOUT_SHADER_READ_ONLY_OPTIMAL,
                       VK_PIPELINE_STAGE_HOST_BIT, VK_ACCESS_HOST_WRITE_BIT, VK_PIPELINE_STAGE_FRAGMENT_SHADER_BIT,
                       VK_ACCESS_SHADER_READ_BIT);
         end_submit(cmd);
      } else {
         tex = upload_tex(iw, ih, tf, 1, 1, vkm_tex_val);
      }
      if (sm == 0) {
         texs[v] = tex;
         if (v == 0)
            texminperf_first = tex;
      }
      /* uv over the 800x600 texel window, steps of one texel */
      const float su = (float)TW / iw, sv = (float)TH / ih;
      struct buf vb = newbuf(6 * 16, VK_BUFFER_USAGE_VERTEX_BUFFER_BIT);
      for (int i = 0; i < 6; i++) {
         const float d[4] = { qp[i][0], qp[i][1], (qp[i][0] + 1) / 2 * su, (qp[i][1] + 1) / 2 * sv };
         memcpy((float *)vb.p + 4 * i, d, 16);
      }
      struct buf u = newbuf(256, VK_BUFFER_USAGE_UNIFORM_BUFFER_BIT);
      ((float *)u.p)[0] = 1.0f / iw;
      ((float *)u.p)[1] = 1.0f / ih;
      VkDescriptorSet set = vkm_set(dsl, &u, 16, &tex, smp);
      const float clear[4] = { 0, 0, 0, 0 };
      /* the timed command buffer has no readback (a CPU copy on the A8); a separate one checks the pixels */
      VkCommandBuffer cmds[2];
      for (int c = 0; c < 2; c++) {
         VkCommandBuffer cmd = cmds[c] = begin(1);
         begin_pass(cmd, rp, fb, W, H, clear, 0);
         vkCmdBindPipeline(cmd, VK_PIPELINE_BIND_POINT_GRAPHICS, p);
         const VkDeviceSize off = 0;
         vkCmdBindVertexBuffers(cmd, 0, 1, &vb.b, &off);
         vkCmdBindDescriptorSets(cmd, VK_PIPELINE_BIND_POINT_GRAPHICS, pl, 0, 1, &set, 0, NULL);
         for (int k = 0; k < 8; k++)
            vkCmdDraw(cmd, 6, 1, 0, 0);
         vkCmdEndRenderPass(cmd);
         if (c)
            readback(cmd, &t, &rb);
         CK(vkEndCommandBuffer(cmd));
      }
      double sum = 0;
      for (int f = 0; f < 11; f++) {
         submit(cmds[f == 0]);
         if (f > 1)
            sum += last_ms;
         if (!f && !nocheck && sm < 2) {
            /* the reference: 15 bilinear taps of one-texel steps at the pixel's uv */
            size_t bad = 0, first = (size_t)-1;
            const uint8_t *g = rb.p;
            for (uint32_t y = 0; y < H; y++)
               for (uint32_t x = 0; x < W; x++) {
                  const double uu = (x + 0.5) / W * TW, vv = (y + 0.5) / H * TH;
                  const int bc[3] = { 2, 1, 0 }; /* target bytes B,G,R from texture channels R,G,B */
                  for (int c = 0; c < 3; c++) {
                     double acc = 0;
                     for (int j = -1; j <= 1; j++)
                        for (int i = -2; i <= 2; i++)
                           acc += bilerp(src, iw, ih, 1024 * 4, uu + i, vv + j, c) * 0.066667;
                     if (fabs(g[4 * (y * W + x) + bc[c]] - acc) > 4) {
                        if (first == (size_t)-1)
                           first = (size_t)y * W + x;
                        bad++;
                        break;
                     }
                  }
               }
            if (bad) {
               printf("RESULT %s FAIL %s, sampler %s: %zu/%u pixels wrong, first (%zu,%zu)\n", rung, names[v],
                      sm ? "trilinear maxLod none" : "maxLod 0", bad, W * H, first % W, first / W);
               finish(1);
            }
         }
      }
      avg[v][sm] = sum / 9; /* submissions 2..10 (1 warms the timed buffer) */
      printf("TEXMIN %s, %s: %.2f ms a job (8 x 15-tap blur over 256x256, minified 800x600)\n", names[v],
             sm == 2 ? "SRGB, trilinear maxLod none" : sm ? "trilinear maxLod none" : "maxLod 0", avg[v][sm]);
   }
   /* does a job cost more with more memory bound (the padded texture's 4 MB against 1.9 MB)? 32 MB more, unused but
    * uploaded once, then the first layout's job again */
   double more = 0;
   {
      struct img big = upload_tex(2048, 2048, VK_FORMAT_R8G8B8A8_UNORM, 1, 1, vkm_tex_val);
      struct img big2 = upload_tex(2048, 2048, VK_FORMAT_R8G8B8A8_UNORM, 1, 1, vkm_tex_val);
      (void)big;
      (void)big2;
      struct buf vb = newbuf(6 * 16, VK_BUFFER_USAGE_VERTEX_BUFFER_BIT);
      for (int i = 0; i < 6; i++) {
         const float d[4] = { qp[i][0], qp[i][1], (qp[i][0] + 1) / 2, (qp[i][1] + 1) / 2 };
         memcpy((float *)vb.p + 4 * i, d, 16);
      }
      struct buf u = newbuf(256, VK_BUFFER_USAGE_UNIFORM_BUFFER_BIT);
      ((float *)u.p)[0] = 1.0f / TW;
      ((float *)u.p)[1] = 1.0f / TH;
      VkDescriptorSet set = vkm_set(dsl, &u, 16, &texminperf_first, smps[0]);
      const float clear[4] = { 0, 0, 0, 0 };
      VkCommandBuffer cmd = begin(1);
      begin_pass(cmd, rp, fb, W, H, clear, 0);
      vkCmdBindPipeline(cmd, VK_PIPELINE_BIND_POINT_GRAPHICS, p);
      const VkDeviceSize off = 0;
      vkCmdBindVertexBuffers(cmd, 0, 1, &vb.b, &off);
      vkCmdBindDescriptorSets(cmd, VK_PIPELINE_BIND_POINT_GRAPHICS, pl, 0, 1, &set, 0, NULL);
      for (int k = 0; k < 8; k++)
         vkCmdDraw(cmd, 6, 1, 0, 0);
      vkCmdEndRenderPass(cmd);
      CK(vkEndCommandBuffer(cmd));
      double sum = 0;
      for (int f = 0; f < 10; f++) {
         submit(cmd);
         if (f)
            sum += last_ms;
      }
      more = sum / 9;
      printf("TEXMIN optimal 800x600 (twiddled, padded), maxLod 0, with 32 MB more images bound: %.2f ms a job\n", more);
   }
   printf("RESULT %s PASS%s ms a job (maxLod 0 / trilinear / SRGB trilinear): twiddled-padded %.2f/%.2f/%.2f, "
          "linear %.2f/%.2f/%.2f, twiddled-pot %.2f/%.2f/%.2f; twiddled-padded with 32 MB more bound %.2f\n", rung,
          nocheck ? " (not checked)" : "", avg[0][0], avg[0][1], avg[0][2], avg[1][0], avg[1][1], avg[1][2], avg[2][0],
          avg[2][1], avg[2][2], more);
   finish(0);
}

/* texvkmark: vkmark's effect2d draw exactly (as recorded: sampler words 08000fff/0 = NEAREST min/mag, REPEAT,
 * maxLod ~0; TextureStep 1/256 = one window pixel; the full-screen quad, uv 0..1), so the 15 taps of a pixel are
 * 3.1 x 2.3 texels apart in its 800x600 one-level texture: sparse point sampling, minified, no mips. Per layout and
 * mode the average job time (8 x overdraw, 9 timed jobs); the UNORM NEAREST pass of each layout is checked against
 * the CPU (nearest texel, wrapped), every pixel. Modes: NEAREST UNORM, NEAREST SRGB (vkmark's format, timed), LINEAR
 * UNORM (2x2 footprints, timed). Layouts: optimal 800x600 one-level (twiddled, padded), the same texels LINEAR-tiled,
 * optimal 1024x1024 one-level (twiddled, unpadded) sampled over the same 800x600 window. */
static void r_texvkmark(void)
{
   const uint32_t W = 256, H = 256, TW = 800, TH = 600;
   VkSampler nearest = newsampler_ex(VK_FILTER_NEAREST, VK_SAMPLER_MIPMAP_MODE_NEAREST, VK_SAMPLER_ADDRESS_MODE_REPEAT, 0);
   VkSampler linear = newsampler_ex(VK_FILTER_LINEAR, VK_SAMPLER_MIPMAP_MODE_NEAREST, VK_SAMPLER_ADDRESS_MODE_REPEAT, 0);
   VkDescriptorSetLayout dsl = vkm_dsl();
   struct img t = newimg_ex(W, H, VK_FORMAT_B8G8R8A8_UNORM, VK_IMAGE_USAGE_COLOR_ATTACHMENT_BIT |
                            VK_IMAGE_USAGE_TRANSFER_SRC_BIT, 1, 1, 0);
   VkRenderPass rp = newrp(VK_FORMAT_UNDEFINED, VK_IMAGE_LAYOUT_TRANSFER_SRC_OPTIMAL);
   VkFramebuffer fb = newfb(rp, &t, NULL);
   struct buf rb = newbuf((VkDeviceSize)W * H * 4, VK_BUFFER_USAGE_TRANSFER_DST_BIT);
   VkPipelineLayout pl = newlayout(dsl);
   const struct pipe_cfg pc = { vkm_shader("effect2d.vert"), vkm_shader("effect2d-blur.frag"), VIN_POS2_TC2, 0, 0, pl, rp,
                                W, H };
   VkPipeline p = newpipe(&pc);
   static const float qp[6][2] = { { -1, -1 }, { 1, -1 }, { -1, 1 }, { -1, 1 }, { 1, -1 }, { 1, 1 } };
   const char *lnames[3] = { "twiddled-padded", "linear", "twiddled-pot" };
   const char *mnames[3] = { "NEAREST UNORM", "NEAREST SRGB", "LINEAR UNORM" };
   double avg[3][3] = { { 0 } };
   uint8_t *src = malloc(1024 * 1024 * 4);
   for (int y = 0; y < 1024; y++)
      for (int x = 0; x < 1024; x++)
         for (int c = 0; c < 4; c++)
            src[4 * (y * 1024 + x) + c] = tmp_val(x, y, c);
   struct buf u = newbuf(256, VK_BUFFER_USAGE_UNIFORM_BUFFER_BIT);
   ((float *)u.p)[0] = 1.0f / W; /* vkmark: one window pixel */
   ((float *)u.p)[1] = 1.0f / H;
   for (int lm = 0; lm < 9; lm++) {
      const int l = lm / 3, m = lm % 3;
      const uint32_t iw = l == 2 ? 1024 : TW, ih = l == 2 ? 1024 : TH;
      const VkFormat tf = m == 1 ? VK_FORMAT_R8G8B8A8_SRGB : VK_FORMAT_R8G8B8A8_UNORM;
      struct img tex;
      if (l == 1) {
         tex = newimg_ex(iw, ih, tf, VK_IMAGE_USAGE_SAMPLED_BIT, 1, 1, 1);
         const VkImageSubresource sub = { VK_IMAGE_ASPECT_COLOR_BIT, 0, 0 };
         VkSubresourceLayout lay;
         vkGetImageSubresourceLayout(dev, tex.i, &sub, &lay);
         uint8_t *map;
         CK(vkMapMemory(dev, tex.m, 0, VK_WHOLE_SIZE, 0, (void **)&map));
         for (uint32_t y = 0; y < ih; y++)
            memcpy(map + lay.offset + y * lay.rowPitch, src + 4 * y * 1024, 4 * iw);
         VkCommandBuffer cmd = begin(0);
         image_barrier(cmd, tex.i, VK_IMAGE_LAYOUT_PREINITIALIZED, VK_IMAGE_LAYOUT_SHADER_READ_ONLY_OPTIMAL,
                       VK_PIPELINE_STAGE_HOST_BIT, VK_ACCESS_HOST_WRITE_BIT, VK_PIPELINE_STAGE_FRAGMENT_SHADER_BIT,
                       VK_ACCESS_SHADER_READ_BIT);
         end_submit(cmd);
      } else {
         tex = upload_tex(iw, ih, tf, 1, 1, vkm_tex_val);
      }
      const float su = (float)TW / iw, sv = (float)TH / ih;
      struct buf vb = newbuf(6 * 16, VK_BUFFER_USAGE_VERTEX_BUFFER_BIT);
      for (int i = 0; i < 6; i++) {
         const float d[4] = { qp[i][0], qp[i][1], (qp[i][0] + 1) / 2 * su, (qp[i][1] + 1) / 2 * sv };
         memcpy((float *)vb.p + 4 * i, d, 16);
      }
      VkDescriptorSet set = vkm_set(dsl, &u, 16, &tex, m == 2 ? linear : nearest);
      const float clear[4] = { 0, 0, 0, 0 };
      VkCommandBuffer cmds[2];
      for (int c = 0; c < 2; c++) {
         VkCommandBuffer cmd = cmds[c] = begin(1);
         begin_pass(cmd, rp, fb, W, H, clear, 0);
         vkCmdBindPipeline(cmd, VK_PIPELINE_BIND_POINT_GRAPHICS, p);
         const VkDeviceSize off = 0;
         vkCmdBindVertexBuffers(cmd, 0, 1, &vb.b, &off);
         vkCmdBindDescriptorSets(cmd, VK_PIPELINE_BIND_POINT_GRAPHICS, pl, 0, 1, &set, 0, NULL);
         vkCmdDraw(cmd, 6, 1, 0, 0); /* one draw a job, as vkmark (overdraw keeps a tile's texels in the cache) */
         vkCmdEndRenderPass(cmd);
         if (c)
            readback(cmd, &t, &rb);
         CK(vkEndCommandBuffer(cmd));
      }
      double sum = 0;
      for (int f = 0; f < 22; f++) {
         submit(cmds[f == 0]);
         if (f > 1)
            sum += last_ms;
         if (!f && !nocheck && m == 0) {
            /* nearest texel of each tap, wrapped (REPEAT) over the image, taps 1/256 apart in uv */
            size_t bad = 0, first = (size_t)-1;
            const uint8_t *g = rb.p;
            for (uint32_t y = 0; y < H; y++)
               for (uint32_t x = 0; x < W; x++) {
                  const int bc[3] = { 2, 1, 0 };
                  for (int c = 0; c < 3; c++) {
                     double acc = 0;
                     for (int j = -1; j <= 1; j++)
                        for (int i = -2; i <= 2; i++) {
                           const double tu = ((x + 0.5) / W) * su + i / (double)W, tv = ((y + 0.5) / H) * sv + j / (double)H;
                           int tx = (int)floor(tu * iw), ty = (int)floor(tv * ih);
                           tx = ((tx % (int)iw) + iw) % iw;
                           ty = ((ty % (int)ih) + ih) % ih;
                           acc += src[4 * (ty * 1024 + tx) + c] * 0.066667;
                        }
                     if (fabs(g[4 * (y * W + x) + bc[c]] - acc) > 3) {
                        if (first == (size_t)-1)
                           first = (size_t)y * W + x;
                        bad++;
                        break;
                     }
                  }
               }
            if (bad) {
               printf("RESULT %s FAIL %s %s: %zu/%u pixels wrong, first (%zu,%zu)\n", rung, lnames[l], mnames[m], bad,
                      W * H, first % W, first / W);
               finish(1);
            }
         }
      }
      avg[l][m] = sum / 20;
      printf("TEXVKMARK %s %s: %.2f ms a job (vkmark's effect2d blur once, 800x600 texels)\n", lnames[l], mnames[m],
             avg[l][m]);
   }
   printf("RESULT %s PASS%s ms a job (NEAREST UNORM / NEAREST SRGB / LINEAR UNORM): twiddled-padded %.2f/%.2f/%.2f, "
          "linear %.2f/%.2f/%.2f, twiddled-pot %.2f/%.2f/%.2f\n", rung, nocheck ? " (not checked)" : "", avg[0][0],
          avg[0][1], avg[0][2], avg[1][0], avg[1][1], avg[1][2], avg[2][0], avg[2][1], avg[2][2]);
   finish(0);
}

/* ---- table: least to most hang risk ---- */
struct rung_desc {
   const char *name, *risk, *what;
};
static const struct rung_desc rungs[] = {
   { "clear64", "low", "clear-only pass 64x64" },
   { "tri64", "low", "the smoke triangle 64x64 (tested)" },
   { "clear256", "low", "clear-only 256x256" },
   { "tri256", "low", "triangle 256x256" },
   { "frames100", "low", "the 64x64 triangle submitted 100 times (one command buffer), fence wait avg/max" },
   { "pc", "low-med", "one rectangle, push-constant colour (VS+FS push constants, 6-vertex corner buffer)" },
   { "draws16", "low-med", "16 rectangles, 16 push-constant colours, one pass" },
   { "vbib", "low-med", "indexed draw (uint16), vec2 position + vec4 colour attributes, varyings" },
   { "ubo", "med", "uniform-buffer colour in the FS (descriptor set)" },
   { "clear1024", "med", "clear-only 1024x1024 (region ring 0x20000, 1024 tiles)" },
   { "clear750x1334", "med", "clear-only at screen size" },
   { "compute64k", "med", "65536 invocations (2048 workgroups), every value checked" },
   { "c2g", "med", "compute writes the triangle's vertices, then the draw reads them" },
   { "timing", "med", "wall-clock ms per job: 10 clears, 10 triangles, 10 dispatches; timestamp bits reported" },
   { "tri1024", "high", "triangle 1024x1024 (the kernel's fixed 64-page PM pool, no grow: running out would need a "
                        "partial render, which the A8 path cannot do yet)" },
   { "tri750x1334", "high", "triangle at screen size (same PM-pool risk)" },
   { "blend", "high", "SRC_ALPHA blend over the left half (tile read in the FS: unproven on the A8)" },
   { "depth32", "high", "D32 attachment, LESS, 3 layered rectangles (A8 ZLS words unproven)" },
   { "depth16", "high", "D16 attachment (SKIP: the A8 driver builds D32F/D32F_S8 only, refused before submit)" },
   { "texnearest", "high", "upload a 64x64 texture, sample NEAREST (A8 texture state unproven on hardware)" },
   { "texlinear", "high", "the same texture, LINEAR into 128x128" },
   { "varyings", "med", "four vertex colours interpolated over two triangles, every pixel against CPU barycentrics" },
   { "texmap", "high", "texel (x,y) encoded in its colour, NEAREST 1:1 (twiddled, 2 levels): which texel each pixel got" },
   { "texmaplin", "high", "texmap with a one-level texture (twiddled on the A8; texhostlinear is linear)" },
   { "texmath", "med", "two samples whose 32-bit results feed arithmetic (average of a texel and its neighbour)" },
   { "texrgba", "med", "an RGBA8 texture (texmap encoding) into the BGRA8 target, NEAREST 1:1" },
   { "texnpot", "med", "a 100x60 texture, twiddled (2 levels), NEAREST 1:1 into 100x60" },
   { "texnpotlin", "med", "the 100x60 texture with one level (twiddled on the A8)" },
   { "texrepeat", "med", "REPEAT, uv 0..1.5 over 64 pixels, every texel index checked" },
   { "texmirror", "med", "MIRRORED_REPEAT, uv 0..1.5" },
   { "texmip", "med-high", "7 levels of one colour each, NEAREST mip: LOD 0..4 into 64..4 pixel targets" },
   { "texhostlinear", "med-high", "a LINEAR-tiling host-visible image written by the CPU at its row pitch, sampled" },
   { "tex2", "med", "two textures in one fragment program (SKIP while the A8 path refuses a second image slot)" },
   { "load2", "high", "clear + left half, then a LOAD pass drawing the top half (Apple's load background, untested)" },
   { "load3", "high", "three passes: clear, LOAD, LOAD (a centre box)" },
   { "load750x1334", "high", "load2 at screen size" },
   { "texdiag", "high", "texnearest with PVR_G5P_TEXDIAG=1: Mesa prints each texture/sampler upload's words and texaddr" },
   { "rtt", "high", "render the 16-colour grid into a texture, sample it in a second pass" },
   { "clearimg", "med", "vkCmdClearColorImage of level 0 to A, then B (a GPU render with a clear background)" },
   { "clearimg750x1334", "med", "clearimg at screen size (twiddled): one clear's job time" },
   { "clearimglin", "med-high", "clearimg into a LINEAR-tiling target (Apple's linear PBE form)" },
   { "clearimglin750x1334", "med-high", "clearimglin at screen size: one clear's job time" },
   { "clearimgmip", "med-high", "clearimg whose first clear covers levels 0 and 1 (a render into mip level 1, untested)" },
   { "vkmtexture", "high", "vkmark's light-basic-tex shaders (its texture scene), 256x256 linear target, every pixel vs CPU" },
   { "vkmeffect2d", "high", "vkmark's effect2d + effect2d-blur shaders (15 taps, UBO in the FS), 256x256 vs CPU" },
   { "vkmdesktop", "high", "vkmark's desktop shaders: background + a rotated half-size window, 256x256 vs CPU" },
   { "texminperf", "high", "minified one-level 800x600 sampling (vkmark effect2d): twiddled-padded vs linear vs twiddled-POT, UNORM and vkmark's SRGB, job ms" },
   { "texvkmark", "high", "vkmark's effect2d draw exactly (NEAREST, REPEAT, taps 1/256 apart): twiddled-padded vs linear vs twiddled-POT job ms" },
   { "mesh", "highest", "one draw of 1k..64k triangles at 256x256 (5 s fence each): which size first hangs (PM pool)" },
   { "meshvary", "highest", "mesh with a vec4 varying per vertex (more parameter memory per vertex)" },
   { "litmesh", "highest", "a lit, depth-tested sphere of 3072 triangles at 256x256 (vkmark vertex/shading without textures)" },
   { "litmeshbig", "highest", "the lit sphere with 24576 triangles (past the 8k knee: the pool-grow regression test)" },
};

int main(int argc, char **argv)
{
   setvbuf(stdout, NULL, _IOLBF, 0);
   if (argc != 2 || !strcmp(argv[1], "list")) {
      printf("rungs, least to most hang risk:\n");
      for (unsigned i = 0; i < sizeof(rungs) / sizeof(rungs[0]); i++)
         printf("  %-14s %-8s %s\n", rungs[i].name, rungs[i].risk, rungs[i].what);
      printf("not built on the A8 (refused by the driver today): MSAA 4x, loadOp LOAD of colour, indexed UBO/SSBO in "
             "graphics shaders, partial render\n");
      return argc == 2 ? 0 : 2;
   }
   rung = argv[1];
   unsigned found = 0;
   for (unsigned i = 0; i < sizeof(rungs) / sizeof(rungs[0]); i++)
      found |= !strcmp(rungs[i].name, rung);
   if (!found) {
      fprintf(stderr, "unknown rung %s (a8-vk-ladder list)\n", rung);
      return 2;
   }
   nocheck = getenv("A8_SMOKE_NOCHECK") != NULL;
   if (!strcmp(rung, "texdiag") || !strcmp(rung, "texmap") || !strcmp(rung, "texmaplin"))
      setenv("PVR_G5P_TEXDIAG", "1", 1); /* before the device: Mesa reads it per draw */
   teardown = getenv("A8_LADDER_TEARDOWN") != NULL;
   t_start = now_ms();
   step("vkCreateInstance");
   if (getenv("A8_LADDER_TIMEOUT_MS"))
      timeout_ns = strtoull(getenv("A8_LADDER_TIMEOUT_MS"), NULL, 0) * 1000000ull;

   const VkApplicationInfo app = { .sType = VK_STRUCTURE_TYPE_APPLICATION_INFO, .apiVersion = VK_API_VERSION_1_1 };
   const VkInstanceCreateInfo ici = { .sType = VK_STRUCTURE_TYPE_INSTANCE_CREATE_INFO, .pApplicationInfo = &app };
   CK(vkCreateInstance(&ici, NULL, &inst));
   step("vkEnumeratePhysicalDevices");
   uint32_t n = 1;
   VkResult r = vkEnumeratePhysicalDevices(inst, &n, &phy);
   if ((r != VK_SUCCESS && r != VK_INCOMPLETE) || !n) {
      printf("RESULT %s FAIL no Vulkan device (%d)\n", rung, r);
      finish(1);
   }
   vkGetPhysicalDeviceMemoryProperties(phy, &memprops);
   const float prio = 1.0f;
   const VkDeviceQueueCreateInfo qi = { .sType = VK_STRUCTURE_TYPE_DEVICE_QUEUE_CREATE_INFO, .queueCount = 1,
                                        .pQueuePriorities = &prio };
   const VkDeviceCreateInfo di = { .sType = VK_STRUCTURE_TYPE_DEVICE_CREATE_INFO, .queueCreateInfoCount = 1,
                                   .pQueueCreateInfos = &qi };
   step("vkCreateDevice");
   CK(vkCreateDevice(phy, &di, NULL, &dev));
   step("device up, rung starts");
   vkGetDeviceQueue(dev, 0, 0, &q);
   const VkCommandPoolCreateInfo cpi = { .sType = VK_STRUCTURE_TYPE_COMMAND_POOL_CREATE_INFO };
   CK(vkCreateCommandPool(dev, &cpi, NULL, &pool));

#define IS(x) !strcmp(rung, x)
   if (IS("clear64")) r_clear(64, 64);
   if (IS("clear256")) r_clear(256, 256);
   if (IS("clear1024")) r_clear(1024, 1024);
   if (IS("clear750x1334")) r_clear(750, 1334);
   if (IS("tri64")) r_tri(64, 64);
   if (IS("tri256")) r_tri(256, 256);
   if (IS("tri1024")) r_tri(1024, 1024);
   if (IS("tri750x1334")) r_tri(750, 1334);
   if (IS("frames100")) r_frames();
   if (IS("pc")) r_pc();
   if (IS("draws16")) r_draws16();
   if (IS("vbib")) r_vbib();
   if (IS("ubo")) r_ubo();
   if (IS("blend")) r_blend();
   if (IS("depth32")) r_depth(VK_FORMAT_D32_SFLOAT);
   if (IS("depth16")) r_depth(VK_FORMAT_D16_UNORM);
   if (IS("texnearest")) r_tex(0);
   if (IS("texlinear")) r_tex(1);
   if (IS("texdiag")) r_tex(0);
   if (IS("texmap")) r_texmap(2);
   if (IS("texmaplin")) r_texmap(1);
   if (IS("varyings")) r_varyings();
   if (IS("texrgba")) r_texrgba();
   if (IS("texnpot")) r_texnpot(2);
   if (IS("texnpotlin")) r_texnpot(1);
   if (IS("texrepeat")) r_texwrap(0);
   if (IS("texmirror")) r_texwrap(1);
   if (IS("texmip")) r_texmip();
   if (IS("texhostlinear")) r_texhostlinear();
   if (IS("tex2")) r_tex2();
   if (IS("load2")) r_load(64, 64, 2);
   if (IS("mesh")) r_mesh(0);
   if (IS("litmesh")) r_litmesh(48, 32);
   if (IS("litmeshbig")) r_litmesh(128, 96);
   if (IS("texmath")) r_texmath();
   if (IS("meshvary")) r_mesh(1);
   if (IS("load3")) r_load(64, 64, 3);
   if (IS("load750x1334")) r_load(750, 1334, 2);
   if (IS("rtt")) r_rtt();
   if (IS("compute64k")) r_compute64k();
   if (IS("c2g")) r_c2g();
   if (IS("timing")) r_timing();
   if (IS("clearimg")) r_clearimg(256, 256, 0);
   if (IS("clearimg750x1334")) r_clearimg(750, 1334, 0);
   if (IS("clearimglin")) r_clearimg(256, 256, 2);
   if (IS("clearimglin750x1334")) r_clearimg(750, 1334, 2);
   if (IS("clearimgmip")) r_clearimg(256, 256, 1);
   if (IS("vkmtexture")) r_vkmark(VKM_TEXTURE);
   if (IS("texminperf")) r_texminperf();
   if (IS("texvkmark")) r_texvkmark();
   if (IS("vkmeffect2d")) r_vkmark(VKM_BLUR);
   if (IS("vkmdesktop")) r_vkmark(VKM_DESKTOP);
   printf("RESULT %s FAIL rung not dispatched\n", rung);
   finish(1);
}
