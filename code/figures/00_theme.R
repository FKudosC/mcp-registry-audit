## 00_theme.R -- shared theme + palette for the Computers & Security manuscript
suppressPackageStartupMessages({
  library(ggplot2); library(dplyr); library(tidyr); library(scales)
  library(patchwork); library(grid)
})

## Okabe-Ito colour-blind-safe palette
OI <- c(blue = "#0072B2", orange = "#E69F00", green = "#009E73",
        vermillion = "#D55E00", purple = "#CC79A7", sky = "#56B4E9",
        yellow = "#F0E442", grey = "#9E9E9E", dark = "#2B2B2B")

PAL <- function(...) unname(OI[c(...)])

## Elsevier-friendly geometry: single column 90 mm, 1.5 column 140 mm, double 190 mm
MM <- function(x) x / 25.4
W1 <- MM(90); W15 <- MM(140); W2 <- MM(190)

BASE_PT <- 7      # base font size (7 pt at final print size)
theme_cs <- function(base_size = BASE_PT, base_family = "sans") {
  theme_bw(base_size = base_size, base_family = base_family) +
    theme(
      panel.border     = element_rect(colour = "black", linewidth = 0.45),
      panel.grid.major = element_line(colour = "grey90", linewidth = 0.25),
      panel.grid.minor = element_blank(),
      axis.text        = element_text(colour = "black", size = base_size),
      axis.title       = element_text(colour = "black", size = base_size),
      axis.ticks       = element_line(colour = "black", linewidth = 0.35),
      axis.ticks.length = unit(1.4, "pt"),
      plot.title       = element_blank(),
      plot.tag         = element_text(size = base_size + 1.5, face = "bold", family = base_family),
      plot.tag.position = c(0, 1),
      legend.key.size  = unit(8, "pt"),
      legend.title     = element_text(size = base_size),
      legend.text      = element_text(size = base_size),
      legend.background = element_blank(),
      legend.key       = element_blank(),
      strip.background = element_rect(fill = "grey95", colour = "black", linewidth = 0.35),
      strip.text       = element_text(size = base_size, colour = "black"),
      plot.margin      = margin(4, 5, 3, 3)
    )
}

## horizontal reference line helper
href <- function(y, ...) geom_hline(yintercept = y, linewidth = 0.3, colour = "grey35", linetype = "dashed")

save_fig <- function(p, name, width, height, outdir = "paper/cs_paper/figures") {
  dir.create(outdir, showWarnings = FALSE, recursive = TRUE)
  pdf_f <- file.path(outdir, paste0(name, ".pdf"))
  png_f <- file.path(outdir, paste0(name, ".png"))
  ggsave(pdf_f, p, width = width, height = height, device = cairo_pdf, bg = "white")
  ggsave(png_f, p, width = width, height = height, dpi = 1200, bg = "white")
  cat(sprintf("  -> %s.pdf / .png  (%.1f x %.1f mm)\n", name, width * 25.4, height * 25.4))
}

## wrap long labels
wrap_lab <- function(x, n = 26) vapply(strwrap(x, width = n, simplify = FALSE), paste, "", collapse = "\n")
