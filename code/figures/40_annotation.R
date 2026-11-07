## 40_annotation.R -- Fig. 12 (annotation reliability and construct validity),
##                    Fig. 11 (mirror owners and the re-ingestion check)
source("paper/cs_paper/R/00_theme.R")
D <- "paper/cs_paper/data"

## ---------------- Fig. 12 : annotation study --------------------------------
kp <- read.csv(file.path(D, "d_kappa.csv"), stringsAsFactors = FALSE)
kp$group <- factor(kp$group, levels = c("Human-Human", "Human-LLM", "LLM-LLM"))
mu <- kp |> group_by(group) |> summarise(m = mean(kappa), .groups = "drop")
pa <- ggplot(kp, aes(kappa, group, colour = group)) +
  geom_segment(aes(x = 0, xend = kappa, yend = group), linewidth = 0.2, colour = "grey80") +
  geom_errorbar(aes(xmin = lo, xmax = hi, colour = group), width = 0.15, linewidth = 0.30,
                show.legend = FALSE) +
  geom_point(size = 1.5) +
  geom_point(data = mu, aes(x = m, y = group), shape = 124, size = 4.2, colour = "black", inherit.aes = FALSE) +
  geom_text(data = mu, aes(x = m, y = group, label = sprintf("%.3f", m)), vjust = -2.0, size = 1.9,
            colour = "black", inherit.aes = FALSE) +
  annotate("text", x = 1.0, y = 1.35, label = "Fleiss \u03ba = 0.672\nKrippendorff \u03b1 = 0.672\n5 raters, n = 600 units",
           size = 1.85, colour = "grey20", hjust = 1, lineheight = 0.95) +
  scale_colour_manual(values = PAL("blue", "green", "purple"), guide = "none") +
  scale_x_continuous(limits = c(0.4, 1.0), breaks = seq(0.4, 1.0, 0.2)) +
  scale_y_discrete(limits = rev(levels(kp$group)), expand = expansion(add = c(0.85, 0.75))) +
  labs(x = "Cohen's \u03ba per rater pair (95% bootstrap interval)", y = NULL) +
  theme_cs() + theme(axis.text.y = element_text(size = BASE_PT - 0.5))

## Panel (b) is the binary version of Table 17(c): the automated band treated as a
## flag for the two irreconcilable classes, against the gold label over the 600 units.
cm <- data.frame(
  auto = factor(c("Flagged", "Flagged", "Not flagged", "Not flagged"), levels = c("Not flagged", "Flagged")),
  gold = factor(c("Irreconcilable", "Other", "Irreconcilable", "Other"), levels = c("Irreconcilable", "Other")),
  n = c(12, 348, 8, 232), lab = c("TP\n12", "FP\n348", "FN\n8", "TN\n232"))
pb <- ggplot(cm, aes(gold, auto, fill = n)) +
  geom_tile(colour = "grey30", linewidth = 0.55) +
  geom_text(aes(label = lab), size = 2.3, lineheight = 0.95, colour = "black") +
  scale_fill_gradient(low = "#F2F2F2", high = "#E69F00", guide = "none") +
  scale_x_discrete(expand = c(0, 0)) + scale_y_discrete(expand = c(0, 0)) +
  # Axis title says "five-rater" because the reference set is a five-reviewer
  # panel, not a single annotator. The shipped PNG raster was patched to match.
  labs(x = "Five-rater reference label (n = 600)", y = "Automated divergence flag",
       caption = "P = 0.033    R = 0.600    F1 = 0.063") +
  theme_cs() + theme(panel.grid = element_blank(),
                     plot.caption = element_text(size = BASE_PT - 0.3, colour = PAL("vermillion"),
                                                 hjust = 0, margin = margin(t = 2)),
                     plot.margin = margin(4, 5, 4, 3))

p12 <- pa + pb + plot_annotation(tag_levels = "a", tag_prefix = "(", tag_suffix = ")") &
  theme(plot.tag = element_text(size = BASE_PT + 1.5, face = "bold"))
save_fig(p12, "fig13_annotation", W2, MM(68))

## ---------------- Fig. 11 : mirror owners + re-ingestion check ---------------
fo <- read.csv(file.path(D, "d_farm_owners.csv"), stringsAsFactors = FALSE)
fo$owner <- factor(fo$owner, levels = fo$owner[order(fo$units)])
## Owners are shown pseudonymously: M-1..M-10 in descending order of units, matching Table 16.
fo$label <- factor(paste0("M-", rank(-fo$units, ties.method = "first")),
                   levels = paste0("M-", seq_len(nrow(fo))))
pa11 <- ggplot(fo, aes(units, label)) +
  geom_col(fill = PAL("purple"), colour = "black", linewidth = 0.25, width = 0.62) +
  geom_text(aes(label = format(units, big.mark = ",")), hjust = -0.14, size = 1.8, colour = "grey15") +
  annotate("text", x = 1480, y = 3.3, label = "top-10 owners cover\n14.9% of all units", size = 1.85,
           colour = "grey20", lineheight = 0.95, hjust = 1) +
  scale_x_continuous(limits = c(0, 1500), breaks = seq(0, 1500, 500),
                     labels = label_number(big.mark = ","),
                     expand = expansion(mult = c(0, 0.02))) +
  scale_y_discrete(limits = rev(levels(fo$label))) +
  labs(x = "Cross-source units under the owner", y = NULL) +
  theme_cs() + theme(axis.text.y = element_text(size = BASE_PT - 1))

## Panel (b) shows both quantities of Table 15 on one shared 0-100% axis as a
## horizontal point-and-line panel, so the two subfigures stay free of facet
## strips and align to a single height and near-equal width.
ri <- read.csv(file.path(D, "d_reingest.csv"), stringsAsFactors = FALSE)
ri$setting <- factor(ri$setting, levels = c("Baseline, same day (7)", "Re-ingestion (2 renamed)"),
                     labels = c("Baseline\n(7 registries)", "Re-ingestion\n(2 renamed)"))
rl <- ri |> select(setting, `Cross-source divergence rate` = rate_pct,
                   `Description carried by one source only` = desc_single_pct) |>
  pivot_longer(-setting, names_to = "metric", values_to = "value")
rl$metric <- factor(rl$metric,
                     levels = c("Cross-source divergence rate",
                                "Description carried by one source only"),
                     labels = c("Cross-source divergence",
                                "Single-source description"))
rl$txt <- ifelse(rl$value < 1, sprintf("%.2f%%", rl$value), sprintf("%.1f%%", rl$value))
pb11 <- ggplot(rl, aes(value, setting, colour = metric, shape = metric, group = metric)) +
  geom_line(linewidth = 0.45, position = position_dodge(width = 0.16)) +
  geom_point(size = 1.9, position = position_dodge(width = 0.16)) +
  geom_text(aes(label = txt), position = position_dodge(width = 0.16),
            hjust = -0.18, size = 1.8, colour = "grey15", show.legend = FALSE) +
  scale_y_discrete(limits = rev(levels(rl$setting))) +
  scale_x_continuous(limits = c(0, 100), breaks = seq(0, 100, 20),
                     labels = function(x) paste0(x, "%"), expand = expansion(mult = c(0, 0.08))) +
  scale_colour_manual(values = c("Cross-source divergence" = PAL("blue"),
                                 "Single-source description" = PAL("orange")), name = NULL) +
  scale_shape_manual(values = c("Cross-source divergence" = 16,
                                "Single-source description" = 17), name = NULL) +
  labs(x = "Percentage of units", y = NULL) +
  theme_cs() +
  theme(legend.position = "bottom", legend.direction = "horizontal",
        legend.key.height = unit(6, "pt"), legend.margin = margin(-2, 0, 0, 0))

p11 <- cowplot::plot_grid(
  pa11, pb11, nrow = 1, align = "hv", axis = "tblr", rel_widths = c(1, 1),
  labels = c("(a)", "(b)"), label_size = BASE_PT + 1.5, label_fontface = "bold",
  label_x = 0, label_y = 1, hjust = -0.2, vjust = 1.25)
save_fig(p11, "fig12_ecosystem", W2, MM(68))
