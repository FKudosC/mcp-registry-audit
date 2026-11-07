## 20_divergence.R -- Fig. 4 (CSCD), Fig. 5 (robustness), Fig. 6 (visibility bound)
source("paper/cs_paper/R/00_theme.R")
D <- "paper/cs_paper/data"

## ---------------- Fig. 4 : CSCD ---------------------------------------------
bands <- read.csv(file.path(D, "d_bands.csv"), stringsAsFactors = FALSE)
bands$units <- as.numeric(bands$units)
LV <- c("Divergent", "Partial", "Near-duplicate", "Identical", "Not comparable")
COLS <- c("Divergent" = PAL("vermillion"), "Partial" = PAL("orange"),
          "Near-duplicate" = PAL("sky"), "Identical" = PAL("green"), "Not comparable" = "grey82")
tot <- sum(bands$units); comp <- sum(bands$units[bands$band != "Not comparable"])
mk <- function(df, denom, y) {
  df <- df[order(factor(df$band, levels = LV)), ]
  df$w <- df$units / denom; df$xmax <- cumsum(df$w); df$xmin <- df$xmax - df$w; df$y <- y; df
}
bb <- rbind(mk(bands, tot, 1), mk(bands[bands$band != "Not comparable", ], comp, 0))
bb$lab <- ifelse(bb$w >= 0.07, sprintf("%.1f%%", 100 * bb$w), "")
pa <- ggplot() +
  geom_rect(data = bb, aes(xmin = xmin, xmax = xmax, ymin = y - 0.16, ymax = y + 0.16, fill = band),
            colour = "black", linewidth = 0.25) +
  geom_text(data = bb, aes(x = (xmin + xmax) / 2, y = y, label = lab), size = 1.95, colour = "white") +
  scale_fill_manual(values = COLS, name = NULL, breaks = LV) +
  scale_x_continuous(limits = c(0, 1), breaks = seq(0, 1, 0.25),
                     labels = function(x) paste0(x * 100, "%"), expand = expansion(mult = c(0.012, 0.012))) +
  scale_y_continuous(limits = c(-0.62, 1.78), breaks = c(0, 1),
                     labels = c("Comparable\ndescriptions\n(n = 6,928)", "All aligned units\n(n = 22,104)")) +
  labs(x = "Share of units", y = NULL) +
  annotate("text", x = 0.42, y = 0.40, label = "80.4% of comparable\ndescriptions diverge",
           size = 2.05, colour = PAL("vermillion"), lineheight = 0.95, hjust = 0) +
  annotate("text", x = 0.54, y = 1.40, label = "68.7% of aligned units\nhave no cross-source\ndescription pair",
           size = 2.05, colour = "grey25", lineheight = 0.95, hjust = 0) +
  theme_cs() +
  theme(panel.grid.major.y = element_blank(), axis.ticks.y = element_blank(),
        axis.text.y = element_text(lineheight = 0.92, size = BASE_PT - 0.5),
        legend.position = "inside", legend.position.inside = c(0.012, 0.985),
        legend.justification = c(0, 1), legend.direction = "vertical",
        legend.key.height = unit(6, "pt"), legend.text = element_text(size = BASE_PT - 0.6))

sp <- read.csv(file.path(D, "d_span2.csv"), stringsAsFactors = FALSE)
sp$label <- factor(sp$label, levels = c("Same day", "1-2 days apart", "3+ days apart"),
                   labels = c("Same day", "1-2 days", "3+ days"))
sp$group <- factor(sp$group, levels = c("Bands", "Like-for-like"),
                   labels = c("All units", "Same source sets"))
## Both panels are drawn without facet strips. Panel (b) is a horizontal grouped
## bar chart of the same band/like-for-like rates; cowplot aligns the two plot
## areas so the subfigures share one height and near-equal widths.
pb <- ggplot(sp, aes(rate_pct, label, fill = group)) +
  geom_col(position = position_dodge(width = 0.72), width = 0.62,
           colour = "black", linewidth = 0.25) +
  geom_text(aes(label = sprintf("%.1f%%", rate_pct)), position = position_dodge(width = 0.72),
            hjust = -0.12, size = 1.8, colour = "grey10") +
  geom_text(aes(label = paste0("n = ", format(comparable, big.mark = ","))),
            position = position_dodge(width = 0.72), hjust = 1.15, size = 1.5, colour = "white") +
  scale_y_discrete(limits = rev(levels(sp$label))) +
  scale_x_continuous(limits = c(0, 100), breaks = seq(0, 100, 25),
                     labels = function(x) paste0(x, "%"), expand = expansion(mult = c(0, 0.10))) +
  scale_fill_manual(values = c("All units" = PAL("blue"), "Same source sets" = PAL("purple")),
                    name = NULL) +
  labs(x = "Divergent rate", y = NULL) +
  theme_cs() +
  theme(legend.position = "bottom", legend.direction = "horizontal",
        legend.key.height = unit(6, "pt"), legend.margin = margin(-2, 0, 0, 0))

p4 <- cowplot::plot_grid(
  pa, pb, nrow = 1, align = "hv", axis = "tblr", rel_widths = c(1, 1),
  labels = c("(a)", "(b)"), label_size = BASE_PT + 1.5, label_fontface = "bold",
  label_x = 0, label_y = 1, hjust = -0.2, vjust = 1.25)
save_fig(p4, "fig05_cscd", W2, MM(70))

## ---------------- Fig. 5 : robustness ---------------------------------------
tl <- bind_rows(
  read.csv(file.path(D, "d_tau_same.csv")) |> mutate(scope = "Same-day units (n = 5,420)"),
  read.csv(file.path(D, "d_tau_all.csv"))  |> mutate(scope = "All comparable units (n = 6,928)"))
tl$scope <- factor(tl$scope, levels = c("All comparable units (n = 6,928)", "Same-day units (n = 5,420)"))
pc <- ggplot(tl, aes(tau, rate_pct, colour = scope, shape = scope)) +
  geom_line(linewidth = 0.5) + geom_point(size = 1.5) +
  geom_text(data = tl,
            aes(label = sprintf("%.1f%%", rate_pct),
                vjust = ifelse(scope == "Same-day units (n = 5,420)", -0.9, 1.9),
                hjust = ifelse(tau == 0.4, -0.12,
                               ifelse(tau == 0.6, 1.12, 0.5))),
            size = 1.7, show.legend = FALSE) +
  scale_colour_manual(values = c("All comparable units (n = 6,928)" = PAL("blue"),
                                 "Same-day units (n = 5,420)" = PAL("orange")), name = NULL) +
  scale_shape_manual(values = c(16, 17), name = NULL) +
  scale_x_continuous(breaks = seq(0.4, 0.6, 0.05)) +
  scale_y_continuous(limits = c(68, 93), labels = function(x) paste0(x, "%")) +
  labs(x = "Similarity threshold \u03c4", y = "Divergent rate") +
  theme_cs() + theme(legend.position = "none")

cp <- bind_rows(
  read.csv(file.path(D, "d_cap_same.csv")) |> mutate(scope = "Same-day units"),
  read.csv(file.path(D, "d_cap_all.csv"))  |> mutate(scope = "All comparable units"))
cp$cap <- factor(cp$cap, levels = c("none", "4x", "3x", "2x"),
                 labels = c("none\n(0.002)", "4\u00d7\n(0.400)", "3\u00d7\n(0.500)", "2\u00d7\n(0.667)"))
pd <- ggplot(cp, aes(cap, rate_pct, fill = scope)) +
  geom_col(position = position_dodge(width = 0.74), width = 0.68, colour = "black", linewidth = 0.25) +
  geom_text(aes(label = sprintf("%.1f%%", rate_pct)), position = position_dodge(width = 0.74),
            vjust = -0.55, size = 1.8) +
  geom_text(aes(label = paste0("n=", format(comparable, big.mark = ","))),
            position = position_dodge(width = 0.74), vjust = 1.8, size = 1.55, colour = "white") +
  annotate("text", x = 3.55, y = 98, label = "3\u00d7 is the cap that removes every pair\nwhose similarity ceiling is below \u03c4 = 0.5",
           size = 1.7, colour = "grey20", lineheight = 0.95, hjust = 0.5, vjust = 1) +
  scale_fill_manual(values = c("All comparable units" = PAL("blue"), "Same-day units" = PAL("orange")), name = NULL) +
  scale_y_continuous(limits = c(0, 100), labels = function(x) paste0(x, "%"),
                     expand = expansion(mult = c(0, 0.04))) +
  labs(x = "Maximum-to-minimum description length ratio\n(second line: the smallest similarity ceiling retained)",
       y = "Divergent rate") +
  theme_cs() + theme(legend.position = "none", axis.text.x = element_text(size = BASE_PT - 1.0))

p5 <- ((pc + pd) + plot_layout(guides = "collect") +
       plot_annotation(tag_levels = "a", tag_prefix = "(", tag_suffix = ")")) &
  theme(plot.tag = element_text(size = BASE_PT + 1.5, face = "bold"),
        legend.position = "bottom", legend.direction = "horizontal",
        legend.key.height = unit(7, "pt"), legend.margin = margin(-2, 0, 0, 0))
save_fig(p5, "fig06_robustness", W2, MM(76))

## ---------------- Fig. 6 : visibility bound V(f) ----------------------------
vis <- read.csv(file.path(D, "d_visibility2.csv"), stringsAsFactors = FALSE)
vis$field <- factor(vis$field, levels = rev(vis$field))
vis$ge2p <- ifelse(vis$ge2 == 0, 1.0, vis$ge2)
vis$ge2lab_v <- ifelse(vis$ge2 == 0, -1.15, ifelse(vis$ge2 < 10, -1.15, 1.22))
p6 <- ggplot(vis, aes(y = field)) +
  geom_segment(aes(x = ge2p, xend = ge1, yend = field), linewidth = 0.5, colour = "grey55") +
  geom_point(aes(x = ge1, colour = "Declared by \u2265 1 registry"), size = 1.6) +
  geom_point(data = vis[vis$ge2 > 0, ], aes(x = ge2, colour = "Declared by \u2265 2 registries"), size = 1.6) +
  geom_point(data = vis[vis$ge2 == 0, ], aes(x = 1.0, colour = "Declared by \u2265 2 registries"),
             shape = 1, size = 1.6, stroke = 0.55) +
  geom_text(aes(x = ge1, label = format(ge1, big.mark = ",")), hjust = -0.25, size = 1.8, colour = "grey15") +
  geom_text(data = vis[vis$ge2 > 0, ], aes(x = ge2, label = format(ge2, big.mark = ",")),
            vjust = -1.35, hjust = 0.5, size = 1.8, colour = "grey15") +
  geom_text(data = vis[vis$ge2 == 0, ], aes(x = 1.0, label = "0"), vjust = -1.35, hjust = 0.5,
            size = 1.8, colour = "grey15") +
  geom_text(aes(x = 1.6e5, label = sprintf("%.1f%%", miss_pct)), hjust = 0, size = 1.85,
            colour = PAL("vermillion")) +
  annotate("text", x = 1.6e5, y = 5.62, label = "carried by one\nregistry only", size = 1.7,
           colour = "grey25", hjust = 0, lineheight = 0.95) +
  scale_x_log10(limits = c(1, 1.1e6), breaks = c(1, 10, 100, 1000, 10000),
                labels = c("1", "10", "100", "1,000", "10,000")) +
  scale_colour_manual(values = c("Declared by \u2265 1 registry" = PAL("orange"),
                                 "Declared by \u2265 2 registries" = PAL("blue")), name = NULL) +
  scale_y_discrete(expand = expansion(add = c(0.62, 1.35))) +
  labs(x = "Cross-source units carrying the field (log scale)", y = NULL) +
  theme_cs() + theme(legend.position = "bottom", legend.direction = "horizontal",
                     legend.key.height = unit(6, "pt"), legend.margin = margin(-2, 0, 0, 0))
save_fig(p6, "fig07_visibility", W15, MM(60))
