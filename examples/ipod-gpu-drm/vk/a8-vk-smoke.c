/* SPDX-License-Identifier: MIT */
/*
 * a8-vk-smoke [probe|compute|triangle|all]: Mesa on the A8 through the real powervr driver (T7000 variant).
 * One RESULT line per step, as a8-gpu-drm-test prints them:
 *   probe     the Vulkan device (name, BVNC from the driver name), queue
 *   compute   k_store_const (32 threads, out[i] = 0x12345678) into a host-visible buffer; pass = 32/32
 *   triangle  metal-triangle into a 64x64 B8G8R8A8 target (two mip levels: the twiddled layout the A8 drew first),
 *             clear (0, 0, 0.2, 1), colour (1, 0.25, 0, 1), copied to a host buffer; pass = every pixel is the
 *             triangle's colour inside it and the clear colour outside (one channel step of tolerance, edge pixels
 *             not judged)
 * The shaders are built in (glslangValidator --vn headers). A8_SMOKE_NOCHECK=1 skips the result checks (e.g. on a fake DRM device:
 * nothing executes). Exit status 0 only when every requested step passed.
 */
#include <stdio.h>
#include <stdlib.h>
#include <math.h>
#include <string.h>
#include <vulkan/vulkan.h>

#include "a8_smoke_kstore.h" /* a8_smoke_kstore[] */
#include "a8_smoke_vs.h"     /* a8_smoke_vs[] */
#include "a8_smoke_fs.h"     /* a8_smoke_fs[] */

static int failed;

#define DO(step, x)                                                                                         \
   do {                                                                                                     \
      VkResult r_ = (x);                                                                                    \
      if (r_ != VK_SUCCESS) {                                                                               \
         printf("RESULT %s FAIL %s = %d\n", step, #x, r_);                                                 \
         fflush(stdout);                                                                                    \
         return 1;                                                                                          \
      }                                                                                                     \
   } while (0)

struct ctx {
   VkInstance inst;
   VkPhysicalDevice phy;
   VkDevice dev;
   VkQueue q;
   VkCommandPool pool;
};

static uint32_t mem_type(const struct ctx *c, uint32_t bits, VkMemoryPropertyFlags want)
{
   VkPhysicalDeviceMemoryProperties mp;
   vkGetPhysicalDeviceMemoryProperties(c->phy, &mp);
   for (uint32_t i = 0; i < mp.memoryTypeCount; i++)
      if ((bits & (1u << i)) && (mp.memoryTypes[i].propertyFlags & want) == want)
         return i;
   return 0;
}

static int host_buffer(const struct ctx *c, const char *step, VkDeviceSize size, VkBufferUsageFlags usage,
                       VkBuffer *buf, VkDeviceMemory *mem, void **map)
{
   const VkBufferCreateInfo bi = { .sType = VK_STRUCTURE_TYPE_BUFFER_CREATE_INFO, .size = size, .usage = usage };
   DO(step, vkCreateBuffer(c->dev, &bi, NULL, buf));
   VkMemoryRequirements mr;
   vkGetBufferMemoryRequirements(c->dev, *buf, &mr);
   const VkMemoryAllocateInfo ai = {
      .sType = VK_STRUCTURE_TYPE_MEMORY_ALLOCATE_INFO, .allocationSize = mr.size,
      .memoryTypeIndex = mem_type(c, mr.memoryTypeBits,
                                  VK_MEMORY_PROPERTY_HOST_VISIBLE_BIT | VK_MEMORY_PROPERTY_HOST_COHERENT_BIT) };
   DO(step, vkAllocateMemory(c->dev, &ai, NULL, mem));
   DO(step, vkBindBufferMemory(c->dev, *buf, *mem, 0));
   DO(step, vkMapMemory(c->dev, *mem, 0, VK_WHOLE_SIZE, 0, map));
   return 0;
}

static int run(const struct ctx *c, const char *step, VkCommandBuffer cmd)
{
   VkFence fence;
   const VkFenceCreateInfo fi = { .sType = VK_STRUCTURE_TYPE_FENCE_CREATE_INFO };
   DO(step, vkCreateFence(c->dev, &fi, NULL, &fence));
   const VkSubmitInfo si = { .sType = VK_STRUCTURE_TYPE_SUBMIT_INFO, .commandBufferCount = 1,
                             .pCommandBuffers = &cmd };
   DO(step, vkQueueSubmit(c->q, 1, &si, fence));
   DO(step, vkWaitForFences(c->dev, 1, &fence, VK_TRUE, 5000000000ull));
   vkDestroyFence(c->dev, fence, NULL);
   return 0;
}

static int begin(const struct ctx *c, const char *step, VkCommandBuffer *cmd)
{
   const VkCommandBufferAllocateInfo ai = { .sType = VK_STRUCTURE_TYPE_COMMAND_BUFFER_ALLOCATE_INFO,
                                            .commandPool = c->pool, .level = VK_COMMAND_BUFFER_LEVEL_PRIMARY,
                                            .commandBufferCount = 1 };
   DO(step, vkAllocateCommandBuffers(c->dev, &ai, cmd));
   const VkCommandBufferBeginInfo bi = { .sType = VK_STRUCTURE_TYPE_COMMAND_BUFFER_BEGIN_INFO };
   DO(step, vkBeginCommandBuffer(*cmd, &bi));
   return 0;
}

static int probe(struct ctx *c, int report)
{
   const VkApplicationInfo app = { .sType = VK_STRUCTURE_TYPE_APPLICATION_INFO, .apiVersion = VK_API_VERSION_1_1 };
   const VkInstanceCreateInfo ici = { .sType = VK_STRUCTURE_TYPE_INSTANCE_CREATE_INFO, .pApplicationInfo = &app };
   DO("probe", vkCreateInstance(&ici, NULL, &c->inst));
   uint32_t n = 1;
   VkResult r = vkEnumeratePhysicalDevices(c->inst, &n, &c->phy);
   if ((r != VK_SUCCESS && r != VK_INCOMPLETE) || !n) {
      printf("RESULT probe FAIL no Vulkan device (%d)\n", r);
      return 1;
   }
   VkPhysicalDeviceProperties props;
   vkGetPhysicalDeviceProperties(c->phy, &props);
   const float prio = 1.0f;
   const VkDeviceQueueCreateInfo qi = { .sType = VK_STRUCTURE_TYPE_DEVICE_QUEUE_CREATE_INFO, .queueCount = 1,
                                        .pQueuePriorities = &prio };
   const VkDeviceCreateInfo di = { .sType = VK_STRUCTURE_TYPE_DEVICE_CREATE_INFO, .queueCreateInfoCount = 1,
                                   .pQueueCreateInfos = &qi };
   DO("probe", vkCreateDevice(c->phy, &di, NULL, &c->dev));
   vkGetDeviceQueue(c->dev, 0, 0, &c->q);
   const VkCommandPoolCreateInfo cpci = { .sType = VK_STRUCTURE_TYPE_COMMAND_POOL_CREATE_INFO };
   DO("probe", vkCreateCommandPool(c->dev, &cpci, NULL, &c->pool));
   if (report)
      printf("RESULT probe PASS device \"%s\" api %u.%u.%u driver %#x\n", props.deviceName,
             VK_API_VERSION_MAJOR(props.apiVersion), VK_API_VERSION_MINOR(props.apiVersion),
             VK_API_VERSION_PATCH(props.apiVersion), props.driverVersion);
   return 0;
}

static int compute(const struct ctx *c, int check)
{
   const char *step = "compute";
   VkBuffer buf;
   VkDeviceMemory mem;
   uint32_t *out;
   if (host_buffer(c, step, 4096, VK_BUFFER_USAGE_STORAGE_BUFFER_BIT, &buf, &mem, (void **)&out))
      return 1;
   memset(out, 0, 4096);

   const VkDescriptorSetLayoutBinding b = { 0, VK_DESCRIPTOR_TYPE_STORAGE_BUFFER, 1, VK_SHADER_STAGE_COMPUTE_BIT };
   const VkDescriptorSetLayoutCreateInfo dli = { .sType = VK_STRUCTURE_TYPE_DESCRIPTOR_SET_LAYOUT_CREATE_INFO,
                                                 .bindingCount = 1, .pBindings = &b };
   VkDescriptorSetLayout dsl;
   DO(step, vkCreateDescriptorSetLayout(c->dev, &dli, NULL, &dsl));
   const VkPipelineLayoutCreateInfo pli = { .sType = VK_STRUCTURE_TYPE_PIPELINE_LAYOUT_CREATE_INFO,
                                            .setLayoutCount = 1, .pSetLayouts = &dsl };
   VkPipelineLayout pl;
   DO(step, vkCreatePipelineLayout(c->dev, &pli, NULL, &pl));
   const VkShaderModuleCreateInfo smi = { .sType = VK_STRUCTURE_TYPE_SHADER_MODULE_CREATE_INFO,
                                          .codeSize = sizeof(a8_smoke_kstore), .pCode = a8_smoke_kstore };
   VkShaderModule sm;
   DO(step, vkCreateShaderModule(c->dev, &smi, NULL, &sm));
   const VkComputePipelineCreateInfo cpi = {
      .sType = VK_STRUCTURE_TYPE_COMPUTE_PIPELINE_CREATE_INFO,
      .stage = { .sType = VK_STRUCTURE_TYPE_PIPELINE_SHADER_STAGE_CREATE_INFO, .stage = VK_SHADER_STAGE_COMPUTE_BIT,
                 .module = sm, .pName = "main" },
      .layout = pl };
   VkPipeline pipe;
   DO(step, vkCreateComputePipelines(c->dev, VK_NULL_HANDLE, 1, &cpi, NULL, &pipe));
   const VkDescriptorPoolSize ps = { VK_DESCRIPTOR_TYPE_STORAGE_BUFFER, 1 };
   const VkDescriptorPoolCreateInfo dpi = { .sType = VK_STRUCTURE_TYPE_DESCRIPTOR_POOL_CREATE_INFO, .maxSets = 1,
                                            .poolSizeCount = 1, .pPoolSizes = &ps };
   VkDescriptorPool dp;
   DO(step, vkCreateDescriptorPool(c->dev, &dpi, NULL, &dp));
   const VkDescriptorSetAllocateInfo dsai = { .sType = VK_STRUCTURE_TYPE_DESCRIPTOR_SET_ALLOCATE_INFO,
                                              .descriptorPool = dp, .descriptorSetCount = 1, .pSetLayouts = &dsl };
   VkDescriptorSet ds;
   DO(step, vkAllocateDescriptorSets(c->dev, &dsai, &ds));
   const VkDescriptorBufferInfo dbi = { buf, 0, VK_WHOLE_SIZE };
   const VkWriteDescriptorSet w = { .sType = VK_STRUCTURE_TYPE_WRITE_DESCRIPTOR_SET, .dstSet = ds,
                                    .descriptorCount = 1, .descriptorType = VK_DESCRIPTOR_TYPE_STORAGE_BUFFER,
                                    .pBufferInfo = &dbi };
   vkUpdateDescriptorSets(c->dev, 1, &w, 0, NULL);

   VkCommandBuffer cmd;
   if (begin(c, step, &cmd))
      return 1;
   vkCmdBindPipeline(cmd, VK_PIPELINE_BIND_POINT_COMPUTE, pipe);
   vkCmdBindDescriptorSets(cmd, VK_PIPELINE_BIND_POINT_COMPUTE, pl, 0, 1, &ds, 0, NULL);
   vkCmdDispatch(cmd, 1, 1, 1);
   const VkMemoryBarrier mb = { .sType = VK_STRUCTURE_TYPE_MEMORY_BARRIER, .srcAccessMask = VK_ACCESS_SHADER_WRITE_BIT,
                                .dstAccessMask = VK_ACCESS_HOST_READ_BIT };
   vkCmdPipelineBarrier(cmd, VK_PIPELINE_STAGE_COMPUTE_SHADER_BIT, VK_PIPELINE_STAGE_HOST_BIT, 0, 1, &mb, 0, NULL,
                        0, NULL);
   DO(step, vkEndCommandBuffer(cmd));
   if (run(c, step, cmd))
      return 1;

   unsigned good = 0;
   for (unsigned i = 0; i < 32; i++)
      good += out[i] == 0x12345678u;
   const unsigned beyond = out[32];
   if (!check)
      printf("RESULT compute DONE (not checked) %u/32 dwords 0x12345678\n", good);
   else if (good == 32 && !beyond)
      printf("RESULT compute PASS 32/32 dwords 0x12345678\n");
   else {
      printf("RESULT compute FAIL %u/32 dwords 0x12345678 (out[0] %#x, out[31] %#x, out[32] %#x)\n", good, out[0],
             out[31], beyond);
      return 1;
   }
   return 0;
}


/* The signed distance in pixels of pixel centre (x, y) from the triangle (-0.5,-0.5) (0.5,-0.5) (0,0.5) on 64x64,
 * positive inside: the smallest of the three edge distances */
static float inside(int x, int y)
{
   const float px = x + 0.5f, py = y + 0.5f;
   const float vx[3] = { 16, 48, 32 }, vy[3] = { 16, 16, 48 };
   float d = 1e9f;
   for (int i = 0; i < 3; i++) {
      const int j = (i + 1) % 3;
      const float ex = vx[j] - vx[i], ey = vy[j] - vy[i];
      const float e = (ex * (py - vy[i]) - ey * (px - vx[i])) / sqrtf(ex * ex + ey * ey);
      d = fminf(d, e);
   }
   return d;
}

static int near(const uint8_t *p, const uint8_t *want)
{
   for (int i = 0; i < 4; i++)
      if (abs((int)p[i] - (int)want[i]) > 1)
         return 0;
   return 1;
}

static int triangle(const struct ctx *c, int check)
{
   const char *step = "triangle";
   const VkFormat fmt = VK_FORMAT_B8G8R8A8_UNORM;
   const VkImageCreateInfo imi = { .sType = VK_STRUCTURE_TYPE_IMAGE_CREATE_INFO, .imageType = VK_IMAGE_TYPE_2D,
                                   .format = fmt, .extent = { 64, 64, 1 }, .mipLevels = 2, .arrayLayers = 1,
                                   .samples = VK_SAMPLE_COUNT_1_BIT, .tiling = VK_IMAGE_TILING_OPTIMAL,
                                   .usage = VK_IMAGE_USAGE_COLOR_ATTACHMENT_BIT | VK_IMAGE_USAGE_TRANSFER_SRC_BIT };
   VkImage img;
   DO(step, vkCreateImage(c->dev, &imi, NULL, &img));
   VkMemoryRequirements mr;
   vkGetImageMemoryRequirements(c->dev, img, &mr);
   const VkMemoryAllocateInfo iai = { .sType = VK_STRUCTURE_TYPE_MEMORY_ALLOCATE_INFO, .allocationSize = mr.size,
                                      .memoryTypeIndex = mem_type(c, mr.memoryTypeBits, 0) };
   VkDeviceMemory imem;
   DO(step, vkAllocateMemory(c->dev, &iai, NULL, &imem));
   DO(step, vkBindImageMemory(c->dev, img, imem, 0));
   const VkImageViewCreateInfo vi = { .sType = VK_STRUCTURE_TYPE_IMAGE_VIEW_CREATE_INFO, .image = img,
                                      .viewType = VK_IMAGE_VIEW_TYPE_2D, .format = fmt,
                                      .subresourceRange = { VK_IMAGE_ASPECT_COLOR_BIT, 0, 1, 0, 1 } };
   VkImageView view;
   DO(step, vkCreateImageView(c->dev, &vi, NULL, &view));

   static const float verts[12] = { -0.5f, -0.5f, 0, 1, 0.5f, -0.5f, 0, 1, 0, 0.5f, 0, 1 };
   VkBuffer vb, rb;
   VkDeviceMemory vmem, rmem;
   void *vmap;
   uint8_t *px;
   if (host_buffer(c, step, sizeof(verts), VK_BUFFER_USAGE_VERTEX_BUFFER_BIT, &vb, &vmem, &vmap) ||
       host_buffer(c, step, 64 * 64 * 4, VK_BUFFER_USAGE_TRANSFER_DST_BIT, &rb, &rmem, (void **)&px))
      return 1;
   memcpy(vmap, verts, sizeof(verts));
   memset(px, 0x5a, 64 * 64 * 4);

   const VkAttachmentDescription ad = { .format = fmt, .samples = VK_SAMPLE_COUNT_1_BIT,
                                        .loadOp = VK_ATTACHMENT_LOAD_OP_CLEAR, .storeOp = VK_ATTACHMENT_STORE_OP_STORE,
                                        .stencilLoadOp = VK_ATTACHMENT_LOAD_OP_DONT_CARE,
                                        .stencilStoreOp = VK_ATTACHMENT_STORE_OP_DONT_CARE,
                                        .initialLayout = VK_IMAGE_LAYOUT_UNDEFINED,
                                        .finalLayout = VK_IMAGE_LAYOUT_TRANSFER_SRC_OPTIMAL };
   const VkAttachmentReference ar = { 0, VK_IMAGE_LAYOUT_COLOR_ATTACHMENT_OPTIMAL };
   const VkSubpassDescription sd = { .pipelineBindPoint = VK_PIPELINE_BIND_POINT_GRAPHICS,
                                     .colorAttachmentCount = 1, .pColorAttachments = &ar };
   const VkRenderPassCreateInfo rpi = { .sType = VK_STRUCTURE_TYPE_RENDER_PASS_CREATE_INFO, .attachmentCount = 1,
                                        .pAttachments = &ad, .subpassCount = 1, .pSubpasses = &sd };
   VkRenderPass rp;
   DO(step, vkCreateRenderPass(c->dev, &rpi, NULL, &rp));
   const VkFramebufferCreateInfo fbi = { .sType = VK_STRUCTURE_TYPE_FRAMEBUFFER_CREATE_INFO, .renderPass = rp,
                                         .attachmentCount = 1, .pAttachments = &view, .width = 64, .height = 64,
                                         .layers = 1 };
   VkFramebuffer fb;
   DO(step, vkCreateFramebuffer(c->dev, &fbi, NULL, &fb));

   VkShaderModule vmod, fmod;
   const VkShaderModuleCreateInfo vmi = { .sType = VK_STRUCTURE_TYPE_SHADER_MODULE_CREATE_INFO,
                                          .codeSize = sizeof(a8_smoke_vs), .pCode = a8_smoke_vs };
   const VkShaderModuleCreateInfo fmi = { .sType = VK_STRUCTURE_TYPE_SHADER_MODULE_CREATE_INFO,
                                          .codeSize = sizeof(a8_smoke_fs), .pCode = a8_smoke_fs };
   DO(step, vkCreateShaderModule(c->dev, &vmi, NULL, &vmod));
   DO(step, vkCreateShaderModule(c->dev, &fmi, NULL, &fmod));
   const VkPipelineShaderStageCreateInfo stages[2] = {
      { .sType = VK_STRUCTURE_TYPE_PIPELINE_SHADER_STAGE_CREATE_INFO, .stage = VK_SHADER_STAGE_VERTEX_BIT,
        .module = vmod, .pName = "main" },
      { .sType = VK_STRUCTURE_TYPE_PIPELINE_SHADER_STAGE_CREATE_INFO, .stage = VK_SHADER_STAGE_FRAGMENT_BIT,
        .module = fmod, .pName = "main" },
   };
   const VkVertexInputBindingDescription vbd = { 0, 16, VK_VERTEX_INPUT_RATE_VERTEX };
   const VkVertexInputAttributeDescription vad = { 0, 0, VK_FORMAT_R32G32B32A32_SFLOAT, 0 };
   const VkPipelineVertexInputStateCreateInfo vis = {
      .sType = VK_STRUCTURE_TYPE_PIPELINE_VERTEX_INPUT_STATE_CREATE_INFO, .vertexBindingDescriptionCount = 1,
      .pVertexBindingDescriptions = &vbd, .vertexAttributeDescriptionCount = 1, .pVertexAttributeDescriptions = &vad };
   const VkPipelineInputAssemblyStateCreateInfo ias = {
      .sType = VK_STRUCTURE_TYPE_PIPELINE_INPUT_ASSEMBLY_STATE_CREATE_INFO,
      .topology = VK_PRIMITIVE_TOPOLOGY_TRIANGLE_LIST };
   const VkViewport vp = { 0, 0, 64, 64, 0, 1 };
   const VkRect2D sc = { { 0, 0 }, { 64, 64 } };
   const VkPipelineViewportStateCreateInfo vps = { .sType = VK_STRUCTURE_TYPE_PIPELINE_VIEWPORT_STATE_CREATE_INFO,
                                                   .viewportCount = 1, .pViewports = &vp, .scissorCount = 1,
                                                   .pScissors = &sc };
   const VkPipelineRasterizationStateCreateInfo rs = {
      .sType = VK_STRUCTURE_TYPE_PIPELINE_RASTERIZATION_STATE_CREATE_INFO, .polygonMode = VK_POLYGON_MODE_FILL,
      .cullMode = VK_CULL_MODE_NONE, .frontFace = VK_FRONT_FACE_COUNTER_CLOCKWISE, .lineWidth = 1.0f };
   const VkPipelineMultisampleStateCreateInfo ms = { .sType = VK_STRUCTURE_TYPE_PIPELINE_MULTISAMPLE_STATE_CREATE_INFO,
                                                     .rasterizationSamples = VK_SAMPLE_COUNT_1_BIT };
   const VkPipelineColorBlendAttachmentState cba = { .colorWriteMask = 0xf };
   const VkPipelineColorBlendStateCreateInfo cbs = { .sType = VK_STRUCTURE_TYPE_PIPELINE_COLOR_BLEND_STATE_CREATE_INFO,
                                                     .attachmentCount = 1, .pAttachments = &cba };
   const VkPipelineLayoutCreateInfo pli = { .sType = VK_STRUCTURE_TYPE_PIPELINE_LAYOUT_CREATE_INFO };
   VkPipelineLayout pl;
   DO(step, vkCreatePipelineLayout(c->dev, &pli, NULL, &pl));
   const VkGraphicsPipelineCreateInfo gpi = {
      .sType = VK_STRUCTURE_TYPE_GRAPHICS_PIPELINE_CREATE_INFO, .stageCount = 2, .pStages = stages,
      .pVertexInputState = &vis, .pInputAssemblyState = &ias, .pViewportState = &vps, .pRasterizationState = &rs,
      .pMultisampleState = &ms, .pColorBlendState = &cbs, .layout = pl, .renderPass = rp };
   VkPipeline pipe;
   DO(step, vkCreateGraphicsPipelines(c->dev, VK_NULL_HANDLE, 1, &gpi, NULL, &pipe));

   VkCommandBuffer cmd;
   if (begin(c, step, &cmd))
      return 1;
   const VkClearValue cv = { .color = { .float32 = { 0.0f, 0.0f, 0.2f, 1.0f } } };
   const VkRenderPassBeginInfo rbi = { .sType = VK_STRUCTURE_TYPE_RENDER_PASS_BEGIN_INFO, .renderPass = rp,
                                       .framebuffer = fb, .renderArea = sc, .clearValueCount = 1,
                                       .pClearValues = &cv };
   vkCmdBeginRenderPass(cmd, &rbi, VK_SUBPASS_CONTENTS_INLINE);
   vkCmdBindPipeline(cmd, VK_PIPELINE_BIND_POINT_GRAPHICS, pipe);
   const VkDeviceSize off = 0;
   vkCmdBindVertexBuffers(cmd, 0, 1, &vb, &off);
   vkCmdDraw(cmd, 3, 1, 0, 0);
   vkCmdEndRenderPass(cmd);
   const VkBufferImageCopy copy = { .imageSubresource = { VK_IMAGE_ASPECT_COLOR_BIT, 0, 0, 1 },
                                    .imageExtent = { 64, 64, 1 } };
   vkCmdCopyImageToBuffer(cmd, img, VK_IMAGE_LAYOUT_TRANSFER_SRC_OPTIMAL, rb, 1, &copy);
   const VkMemoryBarrier mb = { .sType = VK_STRUCTURE_TYPE_MEMORY_BARRIER,
                                .srcAccessMask = VK_ACCESS_TRANSFER_WRITE_BIT,
                                .dstAccessMask = VK_ACCESS_HOST_READ_BIT };
   vkCmdPipelineBarrier(cmd, VK_PIPELINE_STAGE_TRANSFER_BIT, VK_PIPELINE_STAGE_HOST_BIT, 0, 1, &mb, 0, NULL, 0,
                        NULL);
   DO(step, vkEndCommandBuffer(cmd));
   if (run(c, step, cmd))
      return 1;

   /* B8G8R8A8: clear (0, 0, 0.2, 1) = 33 00 00 ff, triangle (1, 0.25, 0, 1) = 00 40 ff ff */
   static const uint8_t clear_px[4] = { 0x33, 0x00, 0x00, 0xff }, tri_px[4] = { 0x00, 0x40, 0xff, 0xff };
   unsigned in_ok = 0, in_n = 0, out_ok = 0, out_n = 0;
   int first_bad = -1;
   for (int y = 0; y < 64; y++)
      for (int x = 0; x < 64; x++) {
         const float d = inside(x, y);
         const uint8_t *p = px + 4 * (64 * y + x);
         if (d > 0.75f) {
            in_n++;
            in_ok += near(p, tri_px);
            if (!near(p, tri_px) && first_bad < 0)
               first_bad = 64 * y + x;
         } else if (d < -0.75f) {
            out_n++;
            out_ok += near(p, clear_px);
            if (!near(p, clear_px) && first_bad < 0)
               first_bad = 64 * y + x;
         }
      }
   const uint8_t *ctr = px + 4 * (64 * 32 + 32), *cor = px;
   if (!check)
      printf("RESULT triangle DONE (not checked) inside %u/%u outside %u/%u\n", in_ok, in_n, out_ok, out_n);
   else if (in_ok == in_n && out_ok == out_n)
      printf("RESULT triangle PASS inside %u/%u outside %u/%u centre %02x%02x%02x%02x corner %02x%02x%02x%02x\n",
             in_ok, in_n, out_ok, out_n, ctr[0], ctr[1], ctr[2], ctr[3], cor[0], cor[1], cor[2], cor[3]);
   else {
      const uint8_t *b = px + 4 * first_bad;
      printf("RESULT triangle FAIL inside %u/%u outside %u/%u centre %02x%02x%02x%02x corner %02x%02x%02x%02x "
             "first bad (%d,%d) %02x%02x%02x%02x\n",
             in_ok, in_n, out_ok, out_n, ctr[0], ctr[1], ctr[2], ctr[3], cor[0], cor[1], cor[2], cor[3],
             first_bad % 64, first_bad / 64, b[0], b[1], b[2], b[3]);
      return 1;
   }
   return 0;
}

int main(int argc, char **argv)
{
   const char *what = argc > 1 ? argv[1] : "all";
   const int check = !getenv("A8_SMOKE_NOCHECK");
   const int all = !strcmp(what, "all");
   if (!all && strcmp(what, "probe") && strcmp(what, "compute") && strcmp(what, "triangle")) {
      fprintf(stderr, "usage: %s [probe|compute|triangle|all]\n", argv[0]);
      return 2;
   }
   setvbuf(stdout, NULL, _IOLBF, 0);
   struct ctx c = { 0 };
   if (probe(&c, all || !strcmp(what, "probe")))
      return 1;
   if ((all || !strcmp(what, "compute")) && compute(&c, check))
      failed = 1;
   if (!failed && (all || !strcmp(what, "triangle")) && triangle(&c, check))
      failed = 1;
   return failed;
}
