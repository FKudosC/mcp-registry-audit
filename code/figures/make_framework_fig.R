# Framework figure, final: the order only.
#
# Three attempts tried to carry the failing statuses inside the boxes and each time the text
# overflowed a 2 cm box. A figure at this width can show the sequence and nothing else; the
# statuses are named in the caption and in the table.

suppressPackageStartupMessages(library(grid))

draw <- function() {
  grid.newpage()
  pushViewport(viewport(xscale = c(0, 100), yscale = c(0, 18), clip = "off"))

  box <- function(x, w, label, fill = "white", fs = 11) {
    grid.roundrect(unit(x, "native"), unit(9, "native"),
                   width = unit(w, "native"), height = unit(11, "native"),
                   r = unit(1.0, "mm"),
                   gp = gpar(fill = fill, col = "grey15", lwd = 1.1))
    grid.text(label, unit(x, "native"), unit(9, "native"),
              gp = gpar(fontsize = fs, lineheight = 1.1))
  }

  arw <- function(x0, x1) {
    grid.segments(unit(x0, "native"), unit(9, "native"),
                  unit(x1, "native"), unit(9, "native"),
                  arrow = arrow(angle = 22, length = unit(1.8, "mm"), type = "closed"),
                  gp = gpar(lwd = 1.1, col = "grey20"))
  }

  names7 <- c("identity", "evidence", "analysis", "declaration",
              "procedure", "exclusion", "comparison")
  xs <- seq(9.5, 91, length.out = 7)
  box(3.4, 6.2, "record", fill = "grey94")
  arw(6.6, xs[1] - 5.4)
  for (i in 1:7) {
    box(xs[i], 10.6, sprintf("S%d\n%s", i - 1, names7[i]))
    if (i > 1) arw(xs[i - 1] + 5.4, xs[i] - 5.4)
  }
  popViewport()
}

for (nm in c("png", "pdf")) {
  if (nm == "png") png("fig6_framework.png", width = 16, height = 3.4, units = "cm",
                       res = 1200, type = "cairo")
  else pdf("fig6_framework.pdf", width = 16/2.54, height = 3.4/2.54)
  draw(); dev.off()
}
cat("wrote fig6_framework.png\n")
