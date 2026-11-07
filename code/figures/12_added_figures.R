## 12_added_figures.R -- Nature-style figures for the first three chapters
##
## Figure A separates protocol interaction from registry-mediated discovery.
## Figure B presents the corpus, the two evidence planes and the audit output.
## Every displayed count is read from the audited CSV tables.
##
## Backend: R grid graphics (exclusive plotting/export backend).
## Output: SVG, PDF, PNG and TIFF at final physical size.

suppressPackageStartupMessages({
  library(grid)
  library(grDevices)
})

if (.Platform$OS.type == "windows") {
  try(grDevices::windowsFonts(Arial = grDevices::windowsFont("Arial")),
      silent = TRUE)
}

REL <- "paper/cs_paper"
D   <- file.path(REL, "data")
OUT <- file.path(REL, "figures")

reg <- read.csv(file.path(D, "d_registry.csv"), stringsAsFactors = FALSE)
fun <- read.csv(file.path(D, "d_funnel.csv"), stringsAsFactors = FALSE)
vis <- read.csv(file.path(D, "d_visibility.csv"), stringsAsFactors = FALSE)
ovg <- read.csv(file.path(D, "d_overgrant2.csv"), stringsAsFactors = FALSE)

N_REG    <- nrow(reg)
N_REC    <- sum(reg$entries)
N_INDEX  <- fun$value[1]
N_DIST   <- fun$value[2]
N_UNIT   <- fun$value[3]
N_FIELDS <- nrow(vis)
N_CLASS  <- nrow(ovg)
N_INSTR  <- 8L
N_FIND   <- N_UNIT * N_INSTR

N_AGG    <- sum(reg$entries[reg$registry %in%
                            c("glama", "mcpso", "aibase", "cursor")])
N_OFF    <- sum(reg$entries[reg$registry == "official"])
N_MKT    <- sum(reg$entries[reg$registry %in%
                            c("smithery", "modelscope")])

fmt <- function(x) formatC(x, format = "d", big.mark = ",")

COL <- c(
  ink          = "#1E292C",
  ink_soft     = "#465458",
  muted        = "#748185",
  faint        = "#AAB3B5",
  rule         = "#D4DCDD",
  rule_light   = "#E9EEEE",
  white        = "#FFFFFF",
  protocol     = "#2D6F9E",
  protocol_lt  = "#F2F7FA",
  discovery    = "#2B776B",
  discovery_lt = "#F1F8F5",
  result       = "#A55D3B",
  result_lt    = "#F9F2ED",
  neutral_lt   = "#F7F9F9"
)

F_FAMILY <- "Arial"
F_MICRO  <- 8.0
F_SMALL  <- 8.0
F_BODY   <- 8.0
F_LABEL  <- 8.0
F_HEAD   <- 8.5
F_BIG    <- 10.8

txt <- function(label, x, y, size = F_BODY, col = "ink", face = 1,
                hjust = 0, vjust = 0.5, family = F_FAMILY) {
  grid.text(label, x = x, y = y, hjust = hjust, vjust = vjust,
            gp = gpar(fontsize = size, col = COL[col], fontface = face,
                      fontfamily = family))
}

seg <- function(x0, y0, x1, y1, col = "rule", lwd = 0.55,
                lty = "solid") {
  grid.lines(c(x0, x1), c(y0, y1),
             gp = gpar(col = COL[col], lwd = lwd, lty = lty,
                       lineend = "butt"))
}

arrow_h <- function(x0, x1, y, col = "ink_soft", lwd = 0.70,
                    head_x = 0.0060, head_y = 0.0080, double = FALSE,
                    lty = "solid") {
  s <- sign(x1 - x0)
  grid.lines(c(x0 + s * head_x * 1.45, x1 - s * head_x * 1.45),
             c(y, y), gp = gpar(col = COL[col], lwd = lwd, lty = lty,
                                lineend = "butt"))
  grid.polygon(
    x = c(x1, x1 - s * head_x, x1 - s * head_x),
    y = c(y, y - head_y, y + head_y),
    gp = gpar(fill = COL[col], col = NA)
  )
  if (double) {
    grid.polygon(
      x = c(x0, x0 + s * head_x, x0 + s * head_x),
      y = c(y, y - head_y, y + head_y),
      gp = gpar(fill = COL[col], col = NA)
    )
  }
}

arrow_v <- function(x, y0, y1, col = "ink_soft", lwd = 0.70,
                    head = 0.0060) {
  s <- sign(y1 - y0)
  grid.lines(c(x, x), c(y0 + s * head * 1.45, y1 - s * head * 1.45),
             gp = gpar(col = COL[col], lwd = lwd, lineend = "butt"))
  grid.polygon(
    x = c(x, x - head * 0.75, x + head * 0.75),
    y = c(y1, y1 - s * head * 1.45, y1 - s * head * 1.45),
    gp = gpar(fill = COL[col], col = NA)
  )
}

dot <- function(x, y, col = "ink", r = 0.0040, fill = TRUE) {
  grid.circle(x, y, r = r,
              gp = gpar(fill = if (fill) COL[col] else NA,
                        col = COL[col], lwd = 0.55))
}

box <- function(x, y, width, height, fill = "white", col = "rule",
                lwd = 0.65, lty = "solid") {
  grid.rect(x, y, width = width, height = height,
            gp = gpar(fill = COL[fill], col = COL[col], lwd = lwd,
                      lty = lty))
}

bar <- function(x, y, width, height = 0.025, fill = "protocol",
                col = NA, lwd = 0) {
  if (width <= 0) return(invisible(NULL))
  grid.rect(x + width / 2, y, width = width, height = height,
            gp = gpar(fill = COL[fill],
                      col = if (is.na(col)) NA else COL[col],
                      lwd = lwd))
}

app_icon <- function(cx, cy, col = "ink", w = 0.040, h = 0.050) {
  box(cx, cy, w, h, "white", col, 0.65)
  dot(cx, cy, col, 0.0030)
}

client_icon <- function(cx, cy, col = "protocol") {
  grid.circle(cx, cy, r = 0.016,
              gp = gpar(fill = COL["white"], col = COL[col], lwd = 0.75))
  grid.circle(cx, cy, r = 0.0060,
              gp = gpar(fill = COL[col], col = NA))
}

server_icon <- function(cx, cy, col = "protocol") {
  ys <- c(cy + 0.018, cy, cy - 0.018)
  for (yy in ys) box(cx, yy, 0.040, 0.014, "white", col, 0.60)
  dot(cx - 0.012, cy + 0.018, col, 0.0021)
  dot(cx - 0.012, cy, col, 0.0021)
  dot(cx - 0.012, cy - 0.018, col, 0.0021)
}

stage_node_h <- function(x, y, label, detail, col,
                         detail_col = "muted", detail_face = 1) {
  grid.circle(x, y, r = 0.0145,
              gp = gpar(fill = COL["white"], col = COL[col], lwd = 0.80))
  grid.circle(x, y, r = 0.0052,
              gp = gpar(fill = COL[col], col = NA))
  txt(label, x, y - 0.078, F_LABEL, "ink", 2, 0.5)
  txt(detail, x, y - 0.125, F_MICRO, detail_col, detail_face, 0.5)
}

draw_protocol_figure <- function() {
  pushViewport(viewport(width = unit(1, "npc"), height = unit(1, "npc"),
                        xscale = c(0, 1), yscale = c(0, 1), clip = "off"))
  grid.rect(x = 0.5, y = 0.5, width = 1, height = 1,
            gp = gpar(fill = COL["white"], col = NA))

  ## Panel a: protocol interaction, which begins after a server is known.
  txt("a", 0.045, 0.925, F_HEAD, "ink", 2, 0)
  txt("MCP interaction", 0.068, 0.925, F_HEAD, "protocol", 2, 0)
  txt("known server", 0.955, 0.925, F_MICRO, "muted", 1, 1)

  box(0.205, 0.675, 0.300, 0.215, "protocol_lt", "protocol", 0.60)
  txt("MCP host", 0.205, 0.835, F_LABEL, "ink", 2, 0.5)
  app_icon(0.120, 0.685, "ink", 0.038, 0.048)
  client_icon(0.290, 0.685, "protocol")
  txt("application", 0.120, 0.605, F_MICRO, "muted", 1, 0.5)
  txt("client", 0.290, 0.605, F_MICRO, "protocol", 1, 0.5)

  box(0.795, 0.675, 0.300, 0.215, "protocol_lt", "protocol", 0.60)
  txt("MCP server", 0.795, 0.835, F_LABEL, "ink", 2, 0.5)
  server_icon(0.715, 0.685, "protocol")
  txt("tools | resources | prompts", 0.755, 0.685,
      F_MICRO, "ink_soft", 1, 0)

  arrow_h(0.355, 0.645, 0.675, "protocol", 0.75, double = TRUE)
  txt("MCP session", 0.500, 0.760, F_SMALL, "protocol", 1, 0.5)
  txt("request / response", 0.500, 0.610, F_MICRO, "muted", 1, 0.5)

  seg(0.045, 0.515, 0.955, 0.515, "rule", 0.45)

  ## Panel b: discovery is a separate claim chain ending in an installed artifact.
  txt("b", 0.045, 0.455, F_HEAD, "ink", 2, 0)
  txt("Registry-mediated discovery", 0.068, 0.455,
      F_HEAD, "discovery", 2, 0)
  txt("outside the protocol", 0.955, 0.455, F_MICRO, "muted", 1, 1)

  nx <- c(0.105, 0.370, 0.635, 0.895)
  ny <- 0.300
  stage_node_h(nx[1], ny, "Consumer", "application", "discovery")
  stage_node_h(nx[2], ny, "Registry record", "third-party claim", "discovery",
               "ink_soft", 2)
  stage_node_h(nx[3], ny, "Repository", "named source", "discovery")
  stage_node_h(nx[4], ny, "Installed artifact", "static clone", "discovery")

  arrow_h(0.145, 0.330, ny, "discovery", 0.70)
  arrow_h(0.410, 0.595, ny, "discovery", 0.70, lty = "dashed")
  arrow_h(0.675, 0.855, ny, "discovery", 0.70)

  txt("query", 0.238, 0.360, F_MICRO, "muted", 1, 0.5)
  txt("names", 0.503, 0.360, F_MICRO, "discovery", 1, 0.5)
  txt("clone", 0.765, 0.360, F_MICRO, "muted", 1, 0.5)

  popViewport()
}

draw_design_figure <- function() {
  F_MICRO <- 5.0
  F_SMALL <- 5.2
  F_BODY  <- 5.6
  F_LABEL <- 6.4
  F_HEAD  <- 8.5

  pushViewport(viewport(width = unit(1, "npc"), height = unit(1, "npc"),
                        xscale = c(0, 1), yscale = c(0, 1), clip = "off"))
  grid.rect(x = 0.5, y = 0.5, width = 1, height = 1,
            gp = gpar(fill = COL["white"], col = NA))

  ## Three evidence stations on one reading line.
  seg(0.335, 0.115, 0.335, 0.875, "rule", 0.45, "dashed")
  seg(0.665, 0.115, 0.665, 0.875, "rule", 0.45, "dashed")

  txt("a", 0.045, 0.925, F_HEAD, "ink", 2, 0)
  txt("Corpus and alignment", 0.068, 0.925, F_HEAD, "discovery", 2, 0)
  txt("b", 0.375, 0.925, F_HEAD, "ink", 2, 0)
  txt("Two evidence planes", 0.398, 0.925, F_HEAD, "protocol", 2, 0)
  txt("c", 0.705, 0.925, F_HEAD, "ink", 2, 0)
  txt("Audit output", 0.728, 0.925, F_HEAD, "result", 2, 0)

  ## Panel a: source composition, then the alignment funnel.
  txt("7 public registries", 0.055, 0.835, F_LABEL, "ink", 2, 0)
  txt(fmt(N_REC), 0.055, 0.750, F_BIG, "ink", 2, 0)
  txt("server records", 0.155, 0.750, F_SMALL, "muted", 1, 0)

  bar_x <- 0.055
  bar_w <- 0.235
  seg_w <- c(N_AGG, N_OFF, N_MKT) / N_REC * bar_w
  bar(bar_x, 0.650, seg_w[1], 0.032, "discovery")
  bar(bar_x + seg_w[1], 0.650, seg_w[2], 0.032, "protocol")
  bar(bar_x + seg_w[1] + seg_w[2], 0.650, seg_w[3], 0.032, "result")
  seg(bar_x + seg_w[1], 0.625, bar_x + seg_w[1], 0.675,
      "white", 0.65)
  seg(bar_x + seg_w[1] + seg_w[2], 0.625,
      bar_x + seg_w[1] + seg_w[2], 0.675, "white", 0.65)

  txt("Aggregators", 0.055, 0.585, F_MICRO, "discovery", 2, 0)
  txt(paste0(fmt(N_AGG), "  ", sprintf("%.1f%%", 100 * N_AGG / N_REC)),
      0.235, 0.585, F_MICRO, "muted", 1, 0)
  txt("Official", 0.055, 0.540, F_MICRO, "protocol", 2, 0)
  txt(paste0(fmt(N_OFF), "  ", sprintf("%.1f%%", 100 * N_OFF / N_REC)),
      0.235, 0.540, F_MICRO, "muted", 1, 0)
  txt("Marketplaces", 0.055, 0.495, F_MICRO, "result", 2, 0)
  txt(paste0(fmt(N_MKT), "  ", sprintf("%.1f%%", 100 * N_MKT / N_REC)),
      0.235, 0.495, F_MICRO, "muted", 1, 0)

  txt("Alignment", 0.055, 0.405, F_LABEL, "ink", 2, 0)
  bar_start <- 0.145
  bar_max <- 0.105
  align_y <- c(0.330, 0.260, 0.190)
  align_v <- c(N_INDEX, N_DIST, N_UNIT)
  align_w <- bar_max * align_v / max(align_v)
  align_lab <- c("index rows", "distinct repositories", "cross-source units")
  align_col <- c("faint", "protocol", "discovery")
  for (i in seq_along(align_v)) {
    bar(bar_start, align_y[i], align_w[i], 0.020, align_col[i])
    txt(align_lab[i], 0.055, align_y[i], F_MICRO, "muted", 1, 0)
    txt(fmt(align_v[i]), 0.290, align_y[i], F_MICRO, "ink", 1, 1)
  }
  arrow_v(0.302, 0.308, 0.282, "faint", 0.55, 0.0040)
  arrow_v(0.302, 0.238, 0.212, "faint", 0.55, 0.0040)

  ## Panel b: declaration corroboration and the code capability surface.
  txt("Declaration plane", 0.375, 0.835, F_LABEL, "ink", 2, 0)
  txt("corroborated by a second registry", 0.375, 0.790,
      F_MICRO, "muted", 1, 0)

  field_y <- c(0.740, 0.665, 0.590, 0.515, 0.440)
  field_w <- 0.095 * vis$v_pct / max(vis$v_pct)
  for (i in seq_len(nrow(vis))) {
    bar(0.500, field_y[i], field_w[i], 0.018, "discovery")
    txt(vis$field[i], 0.375, field_y[i], F_MICRO, "ink_soft", 1, 0)
    txt(sprintf("%.1f%%", vis$v_pct[i]), 0.610, field_y[i],
        F_MICRO, "muted", 1, 0)
  }

  txt("Code plane", 0.375, 0.340, F_LABEL, "ink", 2, 0)
  txt(paste0(N_CLASS, " capability classes"), 0.375, 0.295,
      F_MICRO, "muted", 1, 0)

  cell <- 0.018
  gap <- 0.004
  sq_x <- 0.395 + (seq_len(N_CLASS) - 1L) * (cell + gap)
  for (i in seq_len(N_CLASS)) {
    fill_col <- if (i <= 3L) "discovery" else "protocol"
    grid.rect(sq_x[i] + cell / 2, 0.245, width = cell, height = cell,
              gp = gpar(fill = COL[fill_col], col = NA))
  }
  txt("two declarable", 0.395, 0.155, F_MICRO, "discovery", 2, 0)
  txt("6 no declaring field", 0.505, 0.155, F_MICRO, "protocol", 2, 0)

  ## Panel c: instruments and the resulting finding inventory.
  txt("8 audit instruments", 0.705, 0.835, F_LABEL, "ink", 2, 0)
  inst <- c("Divergence", "Visibility", "Over-grant", "Blind spot",
            "Revision identity", "Owner / phantom", "Defence baseline",
            "Annotation study")
  ix <- c(0.715, 0.845)
  iy <- c(0.750, 0.678, 0.606, 0.534)
  for (i in seq_along(inst)) {
    xx <- ix[((i - 1L) %% 2L) + 1L]
    yy <- iy[((i - 1L) %/% 2L) + 1L]
    txt(as.character(i), xx, yy, F_MICRO, "result", 2, 0)
    txt(inst[i], xx + 0.017, yy, F_MICRO, "ink_soft", 1, 0)
  }

  seg(0.705, 0.465, 0.955, 0.465, "rule_light", 0.75)
  txt("one finding row per instrument per unit", 0.830, 0.420,
      F_MICRO, "muted", 1, 0.5)
  arrow_v(0.830, 0.390, 0.335, "result", 0.70, 0.0055)
  txt(fmt(N_FIND), 0.830, 0.245, F_BIG, "result", 2, 0.5)
  txt("finding rows", 0.830, 0.170, F_LABEL, "ink", 2, 0.5)
  txt(paste0(fmt(N_UNIT), " units x ", N_INSTR, " instruments"),
      0.830, 0.115, F_MICRO, "muted", 1, 0.5)

  popViewport()
}

save_bundle <- function(draw, stem, width_mm, height_mm) {
  w <- width_mm / 25.4
  h <- height_mm / 25.4

  ragg::agg_png(paste0(stem, ".png"), width = w, height = h,
                units = "in", res = 1200, bg = "white")
  draw()
  dev.off()

  svglite::svglite(paste0(stem, ".svg"), width = w, height = h,
                   bg = "white")
  draw()
  dev.off()

  ## The standard PDF device gives stable text traces for the collision audit.
  old_family <- F_FAMILY
  F_FAMILY <<- "sans"
  grDevices::pdf(paste0(stem, ".pdf"), width = w, height = h,
                 family = F_FAMILY, bg = "white", useDingbats = FALSE,
                 onefile = FALSE)
  draw()
  dev.off()
  F_FAMILY <<- old_family

  ragg::agg_tiff(paste0(stem, ".tiff"), width = w, height = h,
                 units = "in", res = 1200, bg = "white")
  draw()
  dev.off()
}

dir.create(OUT, showWarnings = FALSE, recursive = TRUE)

save_bundle(draw_protocol_figure,
            file.path(OUT, "fig01_protocol_discovery"),
            width_mm = 183, height_mm = 82)

save_bundle(draw_design_figure,
            file.path(OUT, "fig02_design_simple"),
            width_mm = 183, height_mm = 88)

cat("  -> fig01_protocol_discovery  (183 x 82 mm)\n")
cat("  -> fig02_design_simple       (183 x 88 mm)\n")
